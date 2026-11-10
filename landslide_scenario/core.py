"""Static infinite-slope limit equilibrium, vertical depth convention.

FS = [c + (gamma*z*cos(beta)^2 - u)*tan(phi)] /
     [gamma*z*sin(beta)*cos(beta)]
u = m*gamma_w*z*cos(beta)^2, m = vertical water-table height / z.
Units: kPa, kN/m^3, metres, degrees. No temporal event probabilities.
"""
from dataclasses import dataclass, asdict
import math
import numpy as np

WATER_WEIGHT = 9.81
SOIL_FIELDS = ('c_kpa', 'root_kpa', 'phi_deg', 'gamma_knm3', 'depth_m', 'm_ratio')
CLASS_LABELS = {
    0: 'Пологий участок: ниже порога анализа',
    1: 'FS < 1: неустойчив в сценарии',
    2: '1 ≤ FS < порога: малый запас',
    3: 'FS ≥ порога: больший запас в сценарии',
    4: 'Круче предела применимости модели',
    255: 'Нет данных / вне области',
}


@dataclass(frozen=True)
class Parameters:
    c_kpa: float = 5.0
    root_kpa: float = 0.0
    phi_deg: float = 30.0
    gamma_knm3: float = 20.0
    depth_m: float = 2.0
    m_ratio: float = 0.5
    min_slope: float = 5.0
    max_slope: float = 60.0
    reserve_threshold: float = 1.3

    def validate(self):
        if any(not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Все параметры должны быть конечными числами.')
        validate_soil({key:getattr(self,key) for key in SOIL_FIELDS})
        if not 0 < self.min_slope < self.max_slope < 90:
            raise ValueError('Нужно 0 < минимальный уклон < максимальный уклон < 90°.')
        if self.reserve_threshold <= 1:
            raise ValueError('Порог отображения запаса должен быть больше 1.')


def validate_soil(values, mask=None):
    for key in SOIL_FIELDS:
        value = np.asarray(values[key], dtype=float)
        if mask is not None and value.ndim:
            value = value[mask]
        if not np.all(np.isfinite(value)):
            raise ValueError(f'Поле {key}: требуется конечное число для каждой ячейки.')
        lower, upper = {
            'c_kpa':(0,10000), 'root_kpa':(0,10000), 'phi_deg':(0,60),
            'gamma_knm3':(WATER_WEIGHT,40), 'depth_m':(0,100), 'm_ratio':(0,1)
        }[key]
        if np.any(value < lower) or np.any(value > upper):
            raise ValueError(f'Поле {key}: допустимый диапазон {lower}–{upper}.')
        if key in ('gamma_knm3','depth_m') and np.any(value <= lower):
            raise ValueError(f'Поле {key} должно быть больше {lower}.')


def factor_of_safety(slope_deg, c_kpa, phi_deg, gamma_knm3, depth_m, m_ratio, root_kpa=0.0):
    """At exactly zero slope FS=+inf (no gravitational shear in this model)."""
    beta = np.radians(np.asarray(slope_deg,dtype=float))
    normal = np.asarray(gamma_knm3)*np.asarray(depth_m)*np.cos(beta)**2
    pore = np.asarray(m_ratio)*WATER_WEIGHT*np.asarray(depth_m)*np.cos(beta)**2
    driving = np.asarray(gamma_knm3)*np.asarray(depth_m)*np.sin(beta)*np.cos(beta)
    strength = np.asarray(c_kpa)+np.asarray(root_kpa)+(normal-pore)*np.tan(np.radians(phi_deg))
    with np.errstate(divide='ignore',invalid='ignore'):
        return np.where(driving>0,strength/driving,np.inf)


def critical_wetness(slope_deg, soil):
    """Raw m at FS=1, not a rainfall amount. Outside [0,1] is retained."""
    beta = np.radians(slope_deg)
    tangent = np.tan(np.radians(soil['phi_deg']))
    normal = soil['gamma_knm3']*soil['depth_m']*np.cos(beta)**2
    driving = soil['gamma_knm3']*soil['depth_m']*np.sin(beta)*np.cos(beta)
    denom = WATER_WEIGHT*soil['depth_m']*np.cos(beta)**2*tangent
    with np.errstate(divide='ignore',invalid='ignore'):
        return np.where(denom>1e-12,
            (soil['c_kpa']+soil['root_kpa']+normal*tangent-driving)/denom,np.nan)


def analyze(dem, dx, dy, parameters=None, soil=None):
    p=parameters or Parameters();p.validate()
    z=np.asarray(dem,dtype=float)
    if z.ndim!=2 or min(z.shape)<3 or not np.any(np.isfinite(z)):
        raise ValueError('Нужна ЦМР не менее 3×3 с корректными высотами.')
    if not all(math.isfinite(v) and v>0 for v in (dx,dy)):
        raise ValueError('Шаг сетки должен быть положительным, в метрах.')
    valid=np.isfinite(z);safe=valid.copy()
    padded=np.pad(valid,1,constant_values=False)
    for dr in range(3):
        for dc in range(3):safe &= padded[dr:dr+z.shape[0],dc:dc+z.shape[1]]
    if not np.any(safe):raise ValueError('Недостаточно корректных соседних ячеек для уклона.')
    gy,gx=np.gradient(np.where(valid,z,0),dy,dx)
    slope=np.degrees(np.arctan(np.hypot(gx,gy)));slope[~safe]=np.nan
    soil=soil or {key:getattr(p,key) for key in SOIL_FIELDS}
    mapped={key:np.broadcast_to(np.asarray(soil[key],dtype=float),z.shape) for key in SOIL_FIELDS}
    validate_soil(mapped,safe)
    eligible=safe & (slope>=p.min_slope) & (slope<=p.max_slope)
    flat=safe & (slope<p.min_slope)
    steep=safe & (slope>p.max_slope)
    arrays={}
    for name,m in [('fs_dry',0.0),('fs_selected',mapped['m_ratio']),('fs_saturated',1.0)]:
        fs=factor_of_safety(slope,mapped['c_kpa'],mapped['phi_deg'],mapped['gamma_knm3'],
                            mapped['depth_m'],m,mapped['root_kpa'])
        arrays[name]=np.where(eligible,fs,np.nan)
    classes=np.full(z.shape,255,dtype=np.uint8);classes[flat]=0;classes[steep]=4
    selected=arrays['fs_selected']
    classes[eligible & (selected<1)]=1
    classes[eligible & (selected>=1) & (selected<p.reserve_threshold)]=2
    classes[eligible & (selected>=p.reserve_threshold)]=3
    critical=critical_wetness(slope,mapped)
    arrays['critical_m']=np.where(eligible,critical,np.nan)
    critical_class=np.full(z.shape,255,dtype=np.uint8)
    critical_class[flat]=0;critical_class[steep]=4
    dry_bad=eligible & (arrays['fs_dry']<1)
    water_sensitive=eligible & ~dry_bad & np.isfinite(critical) & (arrays['fs_saturated']<=1)
    critical_class[dry_bad]=1
    critical_class[water_sensitive]=2
    critical_class[eligible & ~dry_bad & ~water_sensitive]=3
    arrays.update(slope_deg=slope,stability_class=classes,critical_class=critical_class,
                  pore_kpa=np.where(eligible,mapped['m_ratio']*WATER_WEIGHT*mapped['depth_m']*
                                    np.cos(np.radians(slope))**2,np.nan))
    return dict(arrays=arrays,eligible=eligible,safe=safe,
                summary=dict(valid_cells=int(np.count_nonzero(safe)),
                    analyzed_cells=int(np.count_nonzero(eligible)),
                    flat_cells=int(np.count_nonzero(flat)),steep_cells=int(np.count_nonzero(steep)),
                    min_fs=float(np.nanmin(selected)) if np.any(eligible) else None,
                    unstable_area_m2=int(np.count_nonzero(classes==1))*dx*dy,
                    low_reserve_area_m2=int(np.count_nonzero(classes==2))*dx*dy,
                    water_sensitive_area_m2=int(np.count_nonzero(water_sensitive))*dx*dy,
                    relief_m=float(np.nanmax(z)-np.nanmin(z))))
