"""Run every independent Voellmy tracer, with bounded batches and merged envelopes."""
import math

import numpy as np

from avalanche_scenario.core import Parameters, simulate

MAX_RELEASE_CELLS = 200000
MAX_PATH_VERTICES = 1000000


class ReleaseLimitError(ValueError):
    code = 'area_too_large'

    def __init__(self, count):
        super().__init__(f'В найденных очагах {count:,} ячеек, максимум для одного веб-расчёта — {MAX_RELEASE_CELLS:,}. '
                         'Выделите меньший участок или увеличьте шаг сетки. Сам участок и его рельеф сохранены.')
        self.details = {'release_cells': count, 'max_release_cells': MAX_RELEASE_CELLS}


def _reduce_path(path, maximum):
    if len(path) <= maximum:
        return path
    return [path[index] for index in np.linspace(0, len(path)-1, maximum, dtype=int)]


def simulate_batched(dem, release, dx, dy, params=None, progress=None, cancelled=None):
    p = params or Parameters()
    p.validate()
    mask = np.asarray(release, dtype=bool)
    indices = np.flatnonzero(mask)
    count = len(indices)
    if count > MAX_RELEASE_CELLS:
        raise ReleaseLimitError(count)
    if count <= p.max_particles:
        result = simulate(dem, mask, dx, dy, p, progress, cancelled)
        result['summary']['batches'] = 1
        return result
    batches = math.ceil(count/p.max_particles)
    max_vertices = max(2, MAX_PATH_VERTICES//count)
    merged = None
    vectors = {key: [] for key in ('status', 'distance', 'peak', 'initial_x', 'initial_y')}
    counters = ('particles', 'stopped', 'boundary', 'nodata', 'time_limit', 'truncated',
                'release_area_m2', 'release_volume_m3', 'release_mass_kg', 'steps')
    maxima = ('elapsed_s', 'max_speed_m_s', 'max_pressure_kpa', 'max_horizontal_path_m')
    for batch in range(batches):
        if cancelled and cancelled():
            raise InterruptedError('Расчёт отменён.')
        subset = np.zeros_like(mask)
        subset.flat[indices[batch*p.max_particles:(batch+1)*p.max_particles]] = True
        def report(percent):
            if progress:
                progress(100*(batch+percent/100)/batches)
        part = simulate(dem, subset, dx, dy, p, report, cancelled)
        paths = [_reduce_path(path, max_vertices) for path in part['paths']]
        for key in vectors:
            vectors[key].append(part[key])
        if merged is None:
            merged = {key: part[key] for key in ('speed', 'pressure', 'arrival', 'intensity', 'slope', 'safe')}
            merged['paths'] = paths
            merged['summary'] = dict(part['summary'])
        else:
            for key in ('speed', 'pressure'):
                np.fmax(merged[key], part[key], out=merged[key])
            np.fmin(merged['arrival'], part['arrival'], out=merged['arrival'])
            np.maximum(merged['intensity'], part['intensity'], out=merged['intensity'])
            merged['paths'].extend(paths)
            for key in counters:
                merged['summary'][key] += part['summary'][key]
            for key in maxima:
                merged['summary'][key] = max(merged['summary'][key], part['summary'][key])
        del part
    for key, values in vectors.items():
        merged[key] = np.concatenate(values)
    merged['summary'].update(batches=batches, max_particles_per_batch=p.max_particles,
                             path_vertices_per_tracer_max=max_vertices,
                             reached_area_m2=int(np.count_nonzero(np.isfinite(merged['arrival'])))*dx*dy)
    if progress:
        progress(100)
    return merged
