"""FWI1987 bookkeeping, FBP potential behavior and finite-grid fire arrival.

Reference FWI/FBP equations are vendored under GPL-2.0. Optical indices do not
enter the FWI equations and are not converted to ignition probabilities.
"""
from dataclasses import dataclass,asdict
from datetime import date,timedelta
from functools import lru_cache
import heapq
import math
import numpy as np
from .third_party.cffdrs import fwi
from .third_party.cffdrs.models import FBPInput
from .third_party.cffdrs.fire_behaviour_prediction import fire_behaviour_prediction

FUELS={'C2':'C2 · бореальный еловый лес (аналог)', 'C3':'C3 · зрелый сосновый лес (аналог)',
       'D1':'D1 · лиственный лес без листвы (аналог)', 'O1A':'O1A · полёгшая сухая трава',
       'O1B':'O1B · стоящая сухая трава', 'S1':'S1 · порубочные остатки (аналог)',
       'NF':'NF · заданная негорючая область','WA':'WA · вода'}
FUEL_IDS={name:i+1 for i,name in enumerate(FUELS)}
ID_FUELS={v:k for k,v in FUEL_IDS.items()}


@dataclass(frozen=True)
class Parameters:
    fuel_type:str='C2'
    duration_min:float=180.0
    ffmc_initial:float=85.0
    dmc_initial:float=6.0
    dc_initial:float=15.0
    grass_curing:float=80.0
    grass_load:float=.35
    foliar_moisture:float=100.0
    crown_base:float=4.0
    crown_load:float=0.8
    intensity_low:float=2000.0
    intensity_high:float=4000.0
    max_slope_percent:float=200.0

    def validate(self):
        if self.fuel_type not in FUELS or self.fuel_type in ('NF','WA'):
            raise ValueError('Выберите горючий сценарный тип растительности.')
        values={k:v for k,v in asdict(self).items() if k!='fuel_type'}
        if any(not math.isfinite(x) for x in values.values()):raise ValueError('Параметры должны быть конечными числами.')
        if not 0<self.duration_min<=1440:raise ValueError('Продолжительность сценария должна быть от 0 до 1440 мин.')
        if not 0<=self.ffmc_initial<=101 or not 0<=self.dmc_initial<=1000 or not 0<=self.dc_initial<=1000:
            raise ValueError('Начальные коды: FFMC от 0 до 101, DMC/DC от 0 до 1000.')
        if not 0<self.grass_curing<=100 or not 0<self.grass_load<=100:
            raise ValueError('Доля сухой травы 0–100%, запас травы положителен.')
        if not 0<self.foliar_moisture<=120 or not 0<self.crown_base<=100 or not 0<self.crown_load<=20:
            raise ValueError('Проверьте влажность листвы, высоту основания и запас крон.')
        if not 0<self.intensity_low<self.intensity_high:
            raise ValueError('Пороги интенсивности должны быть положительными и возрастающими.')
        if not 0<self.max_slope_percent<=200:raise ValueError('Предельный уклон FBP: от 0 до 200%.')


def normalized_difference(first,second,valid=None):
    a,b=np.asarray(first,dtype=float),np.asarray(second,dtype=float)
    good=np.isfinite(a)&np.isfinite(b)&(a>=0)&(b>=0)&((a+b)>1e-10)
    if valid is not None:good &= valid
    return np.divide(a-b,a+b,out=np.full(np.broadcast(a,b).shape,np.nan),where=good)


def validate_weather(row):
    for key in ('temp_c','rh_pct','wind_kmh','wind_from_deg','rain_mm'):
        try:value=float(row[key])
        except (TypeError,ValueError,KeyError) as exc:raise ValueError('Нет числового метеополя '+key) from exc
        if not math.isfinite(value):raise ValueError('Пропуск в метеоряде: '+key)
    if not -80<=float(row['temp_c'])<=60 or not 0<=float(row['rh_pct'])<=100:
        raise ValueError('Неверная температура или относительная влажность.')
    if not 0<=float(row['wind_kmh'])<=200 or not 0<=float(row['wind_from_deg'])<=360 or float(row['rain_mm'])<0:
        raise ValueError('Неверный ветер или осадки.')
    return date.fromisoformat(row['date'])


def weather_indices(rows,latitude,parameters=None):
    p=parameters or Parameters();p.validate()
    if not rows:raise ValueError('Метеоряд пуст.')
    if not math.isfinite(latitude) or not -80<=latitude<=80:raise ValueError('Широта должна быть от −80 до 80°.')
    ffmc,dmc,dc=p.ffmc_initial,p.dmc_initial,p.dc_initial
    output=[];previous=None
    for row in rows:
        day=validate_weather(row)
        if previous is not None and day!=previous+timedelta(days=1):
            raise ValueError('Для FWI нужен непрерывный ежедневный ряд без дублей и пропусков.')
        previous=day
        t,rh,w,rain=(float(row[k]) for k in ('temp_c','rh_pct','wind_kmh','rain_mm'))
        ffmc=fwi.fine_fuel_moisture_code(ffmc,t,rh,w,rain)
        dmc=fwi.duff_moisture_code(dmc,t,rh,rain,latitude,day.month)
        dc=fwi.drought_code(dc,t,rh,rain,latitude,day.month)
        isi=fwi.initial_spread_index(ffmc,w);bui=fwi.buildup_index(dmc,dc)
        value=fwi.fire_weather_index(isi,bui)
        output.append(dict(row,ffmc=ffmc,dmc=dmc,dc=dc,isi=isi,bui=bui,fwi=value))
    return output


def terrain_derivatives(dem,dx,dy):
    z=np.asarray(dem,dtype=float)
    if z.ndim!=2 or min(z.shape)<3:raise ValueError('ЦМР должна быть сеткой не менее 3×3.')
    valid=np.isfinite(z);safe=valid.copy();pad=np.pad(valid,1,constant_values=False)
    for dr in range(3):
        for dc in range(3):safe &= pad[dr:dr+z.shape[0],dc:dc+z.shape[1]]
    gy,gx=np.gradient(np.where(valid,z,0),dy,dx)
    grade=np.hypot(gx,gy)*100
    # x east, array-y south: downslope bearing measured clockwise from north.
    aspect=np.degrees(np.arctan2(-gx,gy))%360
    grade[~safe]=np.nan;aspect[~safe]=np.nan
    return grade,aspect,safe


def behavior_grid(dem,fuel_ids,dx,dy,weather,day,latitude,longitude,parameters=None,notify=None,cancelled=None):
    p=parameters or Parameters();p.validate();validate_weather(weather)
    if not 0<=float(weather['ffmc'])<=101 or not 0<=float(weather['bui'])<=1000:
        raise ValueError('Коды FFMC/BUI вне диапазона реализации FBP. Проверьте метеоряд и начальные состояния.')
    grade,aspect,safe=terrain_derivatives(dem,dx,dy)
    fuels=np.asarray(fuel_ids,dtype=np.uint8)
    if fuels.shape!=dem.shape:raise ValueError('Сетка горючего не совпадает с ЦМР.')
    too_steep=safe & (grade>p.max_slope_percent)
    known=safe & (fuels>0) & (fuels<255) & ~too_steep
    fields={key:np.full(dem.shape,np.nan) for key in ('head_m_min','back_m_min','flank_m_min','azimuth_deg','intensity_kw_m')}
    # Keep exact physical inputs, but bound retained predictions independently
    # of raster size. Evicted values are recomputed, never approximated.
    @lru_cache(maxsize=4096)
    def prediction(fuel, grade_value, aspect_value):
        inp=FBPInput(fuel_type=fuel,ffmc=weather['ffmc'],bui=weather['bui'],
            ws=float(weather['wind_kmh']),wd=float(weather['wind_from_deg']),
            gs=grade_value,aspect=aspect_value,lat=latitude,lon=longitude,elv=0,
            dj=date.fromisoformat(day).timetuple().tm_yday,hr=p.duration_min/60,
            cc=p.grass_curing,gfl=p.grass_load,cbh=p.crown_base,cfl=p.crown_load,
            fmc=p.foliar_moisture,accel=0)
        result=fire_behaviour_prediction(inp,'All')
        return result.ros,result.bros,result.fros,result.raz,result.hfi
    rr,cc=np.where(known)
    for index,(r,c) in enumerate(zip(rr,cc)):
        if index%256==0:
            if cancelled and cancelled():raise InterruptedError('Расчёт отменён.')
            if notify:notify(index/max(1,len(rr))*100)
        fuel=ID_FUELS.get(int(fuels[r,c]))
        if fuel is None:raise ValueError('Неизвестный код горючего.')
        if fuel in ('NF','WA'):
            for array in fields.values():array[r,c]=0
            continue
        values=prediction(fuel,float(grade[r,c]),float(aspect[r,c]))
        for array,value in zip(fields.values(),values):
            if not math.isfinite(value) or value<0:raise ValueError('Модель FBP вернула некорректный результат.')
            array[r,c]=value
    classes=np.full(dem.shape,255,dtype=np.uint8)
    positive=known & (fields['head_m_min']>0)
    classes[known & ~positive]=0
    intensity=fields['intensity_kw_m']
    classes[positive]=1;classes[positive & (intensity>=p.intensity_low)]=2;classes[positive & (intensity>=p.intensity_high)]=3
    classes[too_steep]=4
    return dict(arrays=fields,intensity_class=classes,known=known,safe=safe,too_steep=too_steep,
                slope_percent=grade,aspect=aspect)


def directional_speed(head,back,flank,angle):
    """Radial intersection of an expanding offset ellipse, not its normal velocity."""
    a=(head+back)/2;center=(head-back)/2;b=flank
    if min(head,back,b)<=0:return 0.0
    cosine,sine=math.cos(angle),math.sin(angle)
    qa=cosine*cosine/(a*a)+sine*sine/(b*b)
    qb=-2*center*cosine/(a*a);qc=center*center/(a*a)-1
    return max(0,(-qb+math.sqrt(max(0,qb*qb-4*qa*qc)))/(2*qa))


def arrival_times(behavior,seeds,dx,dy,duration_min,cancelled=None,notify=None):
    arrays=behavior['arrays'];h=arrays['head_m_min'];b=arrays['back_m_min'];f=arrays['flank_m_min'];az=arrays['azimuth_deg']
    burnable=np.isfinite(h)&(h>0)&np.isfinite(b)&(b>0)&np.isfinite(f)&(f>0)
    if not seeds:raise ValueError('Укажите хотя бы одну точку условного зажигания.')
    arrival=np.full(h.shape,np.inf);queue=[]
    for row,col in seeds:
        if not 0<=row<h.shape[0] or not 0<=col<h.shape[1] or not burnable[row,col]:
            raise ValueError('Очаг находится вне горючих ячеек, в воде, на облаке или за границей. Выберите другой очаг либо уточните карту горючего.')
        arrival[row,col]=0;heapq.heappush(queue,(0,row,col))
    neighbors=[(dr,dc,math.hypot(dc*dx,dr*dy),math.atan2(dc*dx,-dr*dy))
               for dr in (-1,0,1) for dc in (-1,0,1) if dr or dc]
    visits=0;touched_boundary=False;touched_unknown=False
    while queue:
        time,r,c=heapq.heappop(queue)
        if time!=arrival[r,c] or time>duration_min:continue
        visits+=1
        if visits%256==0:
            if cancelled and cancelled():raise InterruptedError('Расчёт отменён.')
            if notify:notify(min(99,time/duration_min*100))
        if r in (0,h.shape[0]-1) or c in (0,h.shape[1]-1):touched_boundary=True
        for dr,dc,distance,bearing in neighbors:
            nr,nc=r+dr,c+dc
            if not 0<=nr<h.shape[0] or not 0<=nc<h.shape[1]:touched_boundary=True;continue
            # No diagonal leaps through water, data voids or a narrow firebreak.
            if dr and dc and (not burnable[r,nc] or not burnable[nr,c]):continue
            if not burnable[nr,nc]:
                if not np.isfinite(h[nr,nc]):touched_unknown=True
                continue
            v1=directional_speed(h[r,c],b[r,c],f[r,c],bearing-math.radians(az[r,c]))
            v2=directional_speed(h[nr,nc],b[nr,nc],f[nr,nc],bearing-math.radians(az[nr,nc]))
            if min(v1,v2)<=0:continue
            candidate=time+.5*distance*(1/v1+1/v2)
            if candidate<=duration_min and candidate<arrival[nr,nc]:
                arrival[nr,nc]=candidate;heapq.heappush(queue,(candidate,nr,nc))
    if cancelled and cancelled():raise InterruptedError('Расчёт отменён.')
    reached=np.isfinite(arrival)
    return dict(arrival=np.where(reached,arrival,np.nan),reached=reached,
                boundary=touched_boundary,unknown=touched_unknown,visited=visits)
