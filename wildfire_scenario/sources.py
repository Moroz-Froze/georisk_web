"""Public COG / STAC and noon-standard-time model weather acquisition."""
import csv
from datetime import date,datetime,time,timedelta,timezone
import json
from pathlib import Path
from urllib.parse import urlencode,urlparse
from urllib.request import Request,HTTPRedirectHandler,build_opener
from zoneinfo import ZoneInfo
import numpy as np
from osgeo import gdal
from .terrain import network_options,transformed_geometry,spatial_ref
from .core import normalized_difference,validate_weather

STAC='https://earth-search.aws.element84.com/v1/search'
COLLECTION='sentinel-2-c1-l2a'
JSON_HOSTS={'earth-search.aws.element84.com','api.open-meteo.com','archive-api.open-meteo.com'}


def trusted_json_url(url):
    parsed=urlparse(url)
    if (parsed.scheme!='https' or parsed.hostname not in JSON_HOSTS or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None,443)):
        raise ValueError('Неподдерживаемый адрес источника данных.')


class TrustedRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        trusted_json_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def get_json(url):
    trusted_json_url(url)
    request=Request(url,headers={'User-Agent':'QGIS-Wildfire-Scenario/0.1.0','Accept':'application/json'})
    try:
        with build_opener(TrustedRedirect()).open(request,timeout=35) as response:
            body=response.read(16*1024*1024+1)
            if len(body)>16*1024*1024:raise ValueError('Ответ источника слишком велик.')
            return json.loads(body)
    except Exception as exc:raise ValueError('Не удалось получить данные от разрешённого источника по сети.') from exc


def load_weather_csv(path):
    with open(path,encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        fields=('date','temp_c','rh_pct','wind_kmh','wind_from_deg','rain_mm')
        if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames) or not set(fields)<=set(reader.fieldnames):
            raise ValueError('CSV должен содержать уникальные поля date, temp_c, rh_pct, wind_kmh, wind_from_deg, rain_mm.')
        rows=[]
        for row in reader:
            if len(rows)>=10000:raise ValueError('Метеоряд слишком длинный (>10 тыс. дней).')
            if None in row:raise ValueError('В строке CSV больше значений, чем заголовков.')
            day=validate_weather(row)
            # Only model inputs cross the export boundary; extra columns may
            # contain spreadsheet formulas and must never enter the export.
            rows.append(dict(date=day.isoformat(),**{key:float(row[key]) for key in fields[1:]}))
    if not rows:raise ValueError('Метеоряд пуст.')
    return rows,dict(kind='user_csv_noon_LST',path=str(path))


def noon_rows(payload,start_day,end_day):
    hourly=payload['hourly'];times=hourly['time'];lookup={int(t):i for i,t in enumerate(times)}
    try:zone=ZoneInfo(payload['timezone'])
    except Exception as exc:raise ValueError('Не удалось определить местное стандартное время метеоданных.') from exc
    rows=[];day=start_day
    keys=('temperature_2m','relative_humidity_2m','wind_speed_10m','wind_direction_10m')
    while day<=end_day:
        local=datetime.combine(day,time(12),tzinfo=zone)
        # FWI1987 uses noon LOCAL STANDARD time, not noon daylight saving time.
        standard=timezone(local.utcoffset()-local.dst())
        stamp=int(datetime.combine(day,time(12),tzinfo=standard).timestamp())
        if stamp not in lookup:raise ValueError('В погодных данных нет полудня за '+day.isoformat())
        index=lookup[stamp]
        rain_indexes=[lookup.get(stamp-3600*k) for k in range(24)]
        if any(i is None for i in rain_indexes):raise ValueError('Нет 24 часов осадков до местного стандартного полудня.')
        values=[hourly[key][index] for key in keys]
        rain=[hourly['precipitation'][i] for i in rain_indexes]
        if any(v is None for v in values+rain):raise ValueError('Пропуски в метеоданных. Нужен непрерывный ряд.')
        row=dict(date=day.isoformat(),temp_c=values[0],rh_pct=values[1],wind_kmh=values[2],
                 wind_from_deg=values[3],rain_mm=sum(rain))
        validate_weather(row);rows.append(row);day+=timedelta(days=1)
    return rows


def download_weather(latitude,longitude,scenario_date,warmup_days=60):
    target=date.fromisoformat(scenario_date);today=datetime.now(timezone.utc).date()
    if target>today+timedelta(days=15):raise ValueError('Автоматическая погода доступна максимум на 15 дней вперёд.')
    first=target-timedelta(days=warmup_days)
    endpoint=('https://archive-api.open-meteo.com/v1/archive' if target<today-timedelta(days=5)
              else 'https://api.open-meteo.com/v1/forecast')
    query=dict(latitude=latitude,longitude=longitude,start_date=(first-timedelta(days=1)).isoformat(),
        end_date=target.isoformat(),hourly='temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation',
        timezone='auto',timeformat='unixtime',wind_speed_unit='kmh')
    url=endpoint+'?'+urlencode(query);payload=get_json(url)
    rows=noon_rows(payload,first,target)
    return rows,dict(kind='Open-Meteo model weather (not station observations)',url=url,
        timezone=payload['timezone'],warmup_days=warmup_days,
        sample_latitude=payload.get('latitude'),sample_longitude=payload.get('longitude'),
        credit='Weather data by Open-Meteo.com (CC BY 4.0); API usage subject to provider terms.',
        documentation='https://open-meteo.com/en/docs')


def write_weather(path,rows):
    keys=list(rows[0])
    with open(path,'w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=keys);writer.writeheader();writer.writerows(rows)


def warp_asset(asset,terrain,categorical=False):
    url=asset['href'];parsed=urlparse(url)
    if (parsed.scheme!='https' or parsed.hostname!='e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com'
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None,443)
            or not parsed.path.startswith('/sentinel-2-c1-l2a/') or not parsed.path.endswith('.tif') or parsed.query or parsed.fragment):
        raise ValueError('Спутниковый каталог вернул неподдерживаемый адрес данных.')
    transform=terrain['transform'];height,width=terrain['dem'].shape
    xmin,dx,_,ymax,_,ndy=transform
    with network_options():
        source=gdal.OpenEx('/vsicurl/'+url,allowed_drivers=['GTiff'])
        if source is None:raise ValueError('Спутниковый канал недоступен.')
        output=gdal.Warp('',source,format='MEM',dstSRS=terrain['projection'],
            outputBounds=(xmin,ymax+height*ndy,xmin+width*dx,ymax),width=width,height=height,
            resampleAlg='near' if categorical else 'bilinear',srcNodata=0,dstNodata=-9999,
            outputType=gdal.GDT_Float32)
        array=output.ReadAsArray().astype(float)
    missing=array==-9999
    if not categorical:
        band=asset.get('raster:bands',[{}])[0]
        if 'scale' not in band or 'offset' not in band:
            raise ValueError('В Sentinel-2 Collection 1 нет коэффициентов отражательной способности.')
        array=array*float(band['scale'])+float(band['offset'])
    array[missing]=np.nan
    return array


def download_sentinel(terrain,scenario_date,directory,lookback_days=60,notify=None,cancelled=None):
    target=date.fromisoformat(scenario_date)
    end=min(target,datetime.now(timezone.utc).date());start=end-timedelta(days=lookback_days)
    # Search the entire modeled grid, including the selected buffer.
    transform=terrain['transform'];rows,cols=terrain['dem'].shape
    xmin,dx,_,ymax,_,ndy=transform;xmax=xmin+cols*dx;ymin=ymax+rows*ndy
    wkt=f'POLYGON (({xmin} {ymin},{xmax} {ymin},{xmax} {ymax},{xmin} {ymax},{xmin} {ymin}))'
    geographic=transformed_geometry(wkt,terrain['projection'],'EPSG:4326')
    bounds=geographic.GetEnvelope()
    query=dict(collections=COLLECTION,bbox=','.join(map(str,(bounds[0],bounds[2],bounds[1],bounds[3]))),
        datetime=start.isoformat()+'T00:00:00Z/'+end.isoformat()+'T23:59:59Z',limit=100)
    request_url=STAC+'?'+urlencode(query);response=get_json(request_url)
    features=response.get('features',[])
    if not features:raise ValueError('За выбранный период нет Sentinel-2 Collection 1. Увеличьте период поиска.')
    groups={}
    for item in features:
        when=item['properties']['datetime'][:10]
        groups.setdefault(when,[]).append(item)
    # Evaluate clear coverage on the AOI, not the tile-wide cloud percentage.
    # Start with dates having low tile cloud cover; retain the actual scene date.
    ranked=sorted(groups,key=lambda day:(min(i['properties'].get('eo:cloud_cover',100) for i in groups[day]),-date.fromisoformat(day).toordinal()))
    best=None
    valid_region=np.isfinite(terrain['dem'])
    for attempt,day in enumerate(ranked[:4]):
        if cancelled and cancelled():raise InterruptedError('Загрузка отменена.')
        if notify:notify(30+attempt*5,'Sentinel-2: проверка облачности за '+day+'…')
        mosaic={key:np.full(terrain['dem'].shape,np.nan) for key in ('red','nir','swir16','scl')}
        used=[]
        for item in sorted(groups[day],key=lambda i:i['properties'].get('eo:cloud_cover',100))[:6]:
            assets=item['assets']
            if not all(key in assets for key in mosaic):continue
            scl=warp_asset(assets['scl'],terrain,True)
            clear=np.isin(scl,[4,5,6,11]) & valid_region
            if not np.any(clear & ~np.isfinite(mosaic['scl'])):continue
            channels={key:warp_asset(assets[key],terrain) for key in ('red','nir','swir16')}
            usable=clear & ~np.isfinite(mosaic['scl'])
            for key in channels:usable &= np.isfinite(channels[key]) & (channels[key]>=0)
            for key in channels:mosaic[key][usable]=channels[key][usable]
            mosaic['scl'][usable]=scl[usable]
            used.append(dict(id=item['id'],datetime=item['properties']['datetime'],
                assets={key:assets[key] for key in ('red','nir','swir16','scl')}))
        coverage=float(np.count_nonzero(np.isfinite(mosaic['scl'])&valid_region)/max(1,np.count_nonzero(valid_region)))
        if best is None or coverage>best[0]:best=(coverage,day,mosaic,used)
        if coverage>=.95:break
    if best is None or best[0]<.2:
        raise ValueError('Мало безоблачных данных Sentinel-2 (<20% области). Измените дату/период или задайте свою карту горючего без спутника.')
    coverage,day,bands,used=best
    # Same-date mosaics only; no invisible mixture of seasons.
    clear=np.isin(bands['scl'],[4,5,6])
    ndvi=normalized_difference(bands['nir'],bands['red'],clear)
    ndmi=normalized_difference(bands['nir'],bands['swir16'],clear)
    return dict(ndvi=ndvi,ndmi=ndmi,scl=bands['scl'],source=dict(
        collection=COLLECTION,search_url=request_url,date=day,age_days=(target-date.fromisoformat(day)).days,
        clear_coverage=coverage,items=used,
        reflectance='Physical reflectance = DN * raster:bands.scale + raster:bands.offset, Collection 1 only.',
        credit='Contains modified Copernicus Sentinel data; ESA / Copernicus, processed by Element 84.',
        documentation='https://github.com/Element84/earth-search'))
