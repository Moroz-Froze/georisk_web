"""Reduced terrain-constrained Voellmy tracers. No QGIS dependencies.

This is neither the viscoelastic SPH method of Soloviev nor a shallow-flow
solver. Flow depth is prescribed, not inferred from the tracer population.
"""
from dataclasses import dataclass, asdict
import math
import numpy as np

G = 9.81


@dataclass(frozen=True)
class Parameters:
    mu: float = 0.25
    xi: float = 1000.0
    flow_depth: float = 1.0
    release_depth: float = 1.0
    density: float = 300.0
    duration: float = 120.0
    time_step: float = 0.10
    pressure_coefficient: float = 1.0
    low_pressure: float = 3.0
    high_pressure: float = 30.0
    max_particles: int = 20000

    def validate(self):
        for key, value in asdict(self).items():
            if not math.isfinite(value):
                raise ValueError(f'Нечисловой параметр: {key}')
        if not 0 <= self.mu <= 2:
            raise ValueError('Коэффициент mu должен быть от 0 до 2.')
        for key in ('xi', 'flow_depth', 'release_depth', 'density', 'duration',
                    'time_step', 'pressure_coefficient', 'low_pressure'):
            if getattr(self, key) <= 0:
                raise ValueError(f'Параметр {key} должен быть положительным.')
        if self.high_pressure <= self.low_pressure:
            raise ValueError('Верхний порог давления должен быть больше нижнего.')
        if self.time_step > 1 or self.duration > 3600:
            raise ValueError('Шаг не более 1 с, длительность не более 3600 с.')
        if not 1 <= self.max_particles <= 50000:
            raise ValueError('Лимит трассеров должен быть от 1 до 50000.')


def risk_values(pressure_kpa, value, people, vulnerability, frequency, presence=1.0):
    """One scenario's event loss and annual expectation, not annual probability."""
    vals = (pressure_kpa, value, people, vulnerability, frequency, presence)
    if any(not math.isfinite(x) for x in vals):
        raise ValueError('Поля риска должны содержать конечные числа.')
    if min(pressure_kpa, value, people, frequency) < 0:
        raise ValueError('Давление, стоимость, население и частота неотрицательны.')
    if not 0 <= vulnerability <= 1 or not 0 <= presence <= 1:
        raise ValueError('Уязвимость и доля присутствия должны быть от 0 до 1.')
    hit = pressure_kpa > 0
    loss = value * vulnerability if hit else 0.0
    affected = people * presence if hit else 0.0
    return dict(loss_event=loss, eal=frequency * loss,
                people_event=affected, people_year=frequency * affected,
                p_event_year=-math.expm1(-frequency))


def _sample(grid, x, y, dx, dy):
    """Bilinear sample in coordinates measured east/south from upper left."""
    rows, cols = grid.shape
    c = np.clip(x / dx - .5, 0, cols - 1)
    r = np.clip(y / dy - .5, 0, rows - 1)
    c0, r0 = np.floor(c).astype(int), np.floor(r).astype(int)
    c1, r1 = np.minimum(c0 + 1, cols - 1), np.minimum(r0 + 1, rows - 1)
    fx, fy = c - c0, r - r0
    return ((1-fy)*((1-fx)*grid[r0, c0] + fx*grid[r0, c1]) +
            fy*((1-fx)*grid[r1, c0] + fx*grid[r1, c1]))


def simulate(dem, release, dx, dy, params=None, progress=None, cancelled=None):
    p = params or Parameters()
    p.validate()
    z = np.asarray(dem, dtype=float)
    mask = np.asarray(release, dtype=bool)
    if z.ndim != 2 or min(z.shape) < 3 or z.shape != mask.shape:
        raise ValueError('ЦМР и маска очага должны быть одной сеткой не менее 3×3.')
    if not math.isfinite(dx) or not math.isfinite(dy) or min(dx, dy) <= 0:
        raise ValueError('Размер ячейки должен быть положительным, в метрах.')
    valid = np.isfinite(z)
    # Exclude the one-cell halo around voids: no interpolation across NoData.
    safe = valid.copy()
    padded = np.pad(valid, 1, constant_values=True)
    for dr in range(3):
        for dc in range(3):
            safe &= padded[dr:dr+z.shape[0], dc:dc+z.shape[1]]
    if np.any(mask & ~safe):
        raise ValueError('Очаг касается NoData или соседних с ним ячеек ЦМР.')
    rr, cc = np.where(mask)
    n = len(rr)
    if not n:
        raise ValueError('Очаг не содержит центров ячеек ЦМР. Укрупните полигон или уменьшите пиксель.')
    if n > p.max_particles:
        raise ValueError(f'В очаге {n} ячеек, лимит {p.max_particles}. Уменьшите очаг или разрешение ЦМР.')
    # Values adjacent to holes are never used; fill avoids propagating NaNs
    # into gradients of otherwise valid terrain.
    zy, zx = np.gradient(np.where(valid, z, 0.0), dy, dx)
    slope = np.degrees(np.arctan(np.hypot(zx, zy)))
    slope[~safe] = np.nan
    x, y = (cc.astype(float)+.5)*dx, (rr.astype(float)+.5)*dy
    initial_x, initial_y = x.copy(), y.copy()
    vx, vy = np.zeros(n), np.zeros(n)
    status = np.zeros(n, dtype=np.int8)  # 0 active/time limit, 1 stopped, 2 boundary, 3 void
    paths = [[(float(xi), float(yi), 0.0)] for xi, yi in zip(x, y)]
    distance = np.zeros(n)
    peak = np.zeros(n)
    max_speed = np.zeros_like(z)
    arrival = np.full_like(z, np.inf)
    arrival[rr, cc] = 0
    t, step, last_saved = 0.0, 0, 0.0
    save_period = max(1.0, p.duration/max(2, min(250, 1000000/n)))
    safe_float = safe.astype(float)
    while t < p.duration and np.any(status == 0):
        if cancelled and cancelled():
            raise InterruptedError('Расчёт отменён.')
        ids = np.flatnonzero(status == 0)
        sx = _sample(zx, x[ids], y[ids], dx, dy)
        sy = _sample(zy, x[ids], y[ids], dx, dy)
        cos_theta = 1/np.sqrt(1+sx*sx+sy*sy)
        # dt limits horizontal displacement to < 0.25 of the smaller cell.
        vmax = float(np.max(np.hypot(vx[ids], vy[ids])))
        dt = min(p.time_step, .25*min(dx, dy)/(vmax+G*p.time_step), p.duration-t)
        ux = vx[ids] - G*sx*cos_theta**2*dt
        uy = vy[ids] - G*sy*cos_theta**2*dt
        speed = np.sqrt(ux*ux+uy*uy+(sx*ux+sy*uy)**2)
        # Split gravity, no-reversal Coulomb friction, implicit quadratic drag.
        shrink = np.maximum(0, 1-p.mu*G*cos_theta*dt/np.maximum(speed, 1e-12))
        ux *= shrink
        uy *= shrink
        after = speed*shrink
        drag = 1 + G*after*dt/(p.xi*p.flow_depth)
        ux /= drag
        uy /= drag
        newx, newy = x[ids]+ux*dt, y[ids]+uy*dt
        inside = ((newx >= 0) & (newy >= 0) &
                  (newx < z.shape[1]*dx) & (newy < z.shape[0]*dy))
        status[ids[~inside]] = 2
        keep = ids[inside]
        nr = np.floor(newy[inside]/dy).astype(int)
        nc = np.floor(newx[inside]/dx).astype(int)
        good = safe[nr, nc]
        # Bilinear stencil must also remain within the safe terrain.
        good &= _sample(safe_float, newx[inside], newy[inside], dx, dy) > .999999
        status[keep[~good]] = 3
        accepted = inside.copy()
        accepted[inside] = good
        moved = ids[accepted]
        distance[moved] += np.hypot(ux[accepted], uy[accepted])*dt
        x[moved], y[moved] = newx[accepted], newy[accepted]
        vx[moved], vy[moved] = ux[accepted], uy[accepted]
        sp = after[accepted]/drag[accepted]
        peak[moved] = np.maximum(peak[moved], sp)
        r, c = np.floor(y[moved]/dy).astype(int), np.floor(x[moved]/dx).astype(int)
        np.maximum.at(max_speed, (r, c), sp)
        np.minimum.at(arrival, (r, c), t+dt)
        # Static particles on subcritical slopes have reached equilibrium.
        stopped = (sp < .001) & (np.hypot(sx[accepted], sy[accepted]) <= p.mu)
        status[moved[stopped]] = 1
        t += dt
        step += 1
        if t-last_saved >= save_period or not np.any(status == 0) or t >= p.duration:
            for i in range(n):
                pt = (float(x[i]), float(y[i]), float(t))
                if pt[:2] != paths[i][-1][:2]:
                    paths[i].append(pt)
            last_saved = t
        if progress and step % 10 == 0:
            progress(100*t/p.duration)
        if step > 1000000:
            raise ValueError('Превышен лимит шагов; сократите длительность расчёта.')
    pressure = p.pressure_coefficient*p.density*max_speed**2/1000
    arrival[~np.isfinite(arrival)] = np.nan
    intensity = np.zeros(z.shape, dtype=np.uint8)
    intensity[pressure > 0] = 1
    intensity[pressure >= p.low_pressure] = 2
    intensity[pressure >= p.high_pressure] = 3
    max_speed[~safe] = np.nan
    pressure[~safe] = np.nan
    intensity[~safe] = 255
    truncated = int(np.count_nonzero((status == 2) | (status == 3)))
    summary = dict(particles=n, elapsed_s=t, steps=step,
                   stopped=int(np.count_nonzero(status == 1)),
                   boundary=int(np.count_nonzero(status == 2)),
                   nodata=int(np.count_nonzero(status == 3)),
                   time_limit=int(np.count_nonzero(status == 0)),
                   truncated=truncated, max_speed_m_s=float(np.nanmax(max_speed)),
                   max_pressure_kpa=float(np.nanmax(pressure)),
                   release_area_m2=n*dx*dy, release_volume_m3=n*dx*dy*p.release_depth,
                   release_mass_kg=n*dx*dy*p.release_depth*p.density,
                   max_horizontal_path_m=float(np.max(distance)),
                   reached_area_m2=int(np.count_nonzero(np.isfinite(arrival)))*dx*dy)
    if progress:
        progress(100)
    return dict(speed=max_speed, pressure=pressure, intensity=intensity,
                arrival=arrival, slope=slope, paths=paths, status=status,
                distance=distance, peak=peak, initial_x=initial_x, initial_y=initial_y,
                summary=summary, safe=safe)
