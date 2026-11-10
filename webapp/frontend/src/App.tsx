import {useEffect,useState} from 'react';
import {Layers,MapPin,SquareDashedMousePointer,Play,ChevronRight,History,ArrowUpRight,LoaderCircle,X,LocateFixed,ChevronDown,Check,Upload,Menu,Plus,Minus,Compass,Maximize,Hand,Trash2,SlidersHorizontal} from 'lucide-react';
import MapView,{type MapAction} from './MapView';
import ResultPanel from './ResultPanel';
import HazardIcon from './HazardIcon';
import {api} from './api';
import type {Bbox,Hazard,Job,Model} from './types';

const NAMES={avalanche:'Лавины',landslide:'Оползни',wildfire:'Лесные пожары'};
const MAIN={avalanche:['mu','xi','flow_depth','release_depth','density','duration'],landslide:['c_kpa','root_kpa','phi_deg','gamma_knm3','depth_m','m_ratio'],wildfire:['fuel_type','duration_min','grass_curing','grass_load']};
const initialExtra={frequency:'',vulnerability:'0.5',repair_cost:'0',release_min_slope:'30',release_max_slope:'50',release_min_relief:'100',release_min_area:'3600',corridor:'15',warmup_days:'60',lookback_days:'60'};
const STATUS:Record<string,string>={completed:'Готово',running:'Выполняется',queued:'В очереди',failed:'Ошибка',cancelled:'Отменён',interrupted:'Прерван'};
function today(){const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}

export default function App(){
  const [models,setModels]=useState<Model[]>([]),[hazard,setHazard]=useState<Hazard>('avalanche');
  const [parameters,setParameters]=useState<Record<string,number|string>>({}),[buffer,setBuffer]=useState(3000),[resolution,setResolution]=useState(30);
  const [bbox,setBbox]=useState<Bbox|null>(null),[focus,setFocus]=useState<Bbox|null>(null),[coordinate,setCoordinate]=useState('94.3, 51.1');
  const [mode,setMode]=useState<'area'|'ignition'|null>(null),[ignitions,setIgnitions]=useState<[number,number][]>([]);
  const [assets,setAssets]=useState<Record<string,{id:string;name:string}>>({}),[uploading,setUploading]=useState('');
  const [date,setDate]=useState(today()),[satellite,setSatellite]=useState(true),[extra,setExtra]=useState(initialExtra);
  const [job,setJob]=useState<Job|null>(null),[jobs,setJobs]=useState<Job[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [layerId,setLayerId]=useState(''),[opacity,setOpacity]=useState(.8),[tab,setTab]=useState<'settings'|'history'|'results'>('settings');
  const [drawerOpen,setDrawerOpen]=useState(false),[action,setAction]=useState<MapAction|null>(null);
  const model=models.find(m=>m.id===hazard),result=job?.result;
  const active=Boolean(job&&['queued','running'].includes(job.state));
  useEffect(()=>{const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')setDrawerOpen(false);};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape);},[]);
  const refresh=()=>api<Job[]>('/jobs').then(setJobs).catch(()=>{});
  useEffect(()=>{api<Model[]>('/catalog').then(setModels).catch(e=>{setError('Не удалось подключиться к серверу: '+e.message);setDrawerOpen(true);});refresh();},[]);
  useEffect(()=>{if(models.length){const first=models[0];setParameters(Object.fromEntries(first.fields.map(f=>[f.key,f.default])));setBuffer(first.buffer);}},[models]);
  useEffect(()=>{
    if(!job||!active)return;
    let stopped=false,timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try{const next=await api<Job>('/jobs/'+job.id);if(stopped)return;setJob(next);
        if(next.result){setLayerId(next.result.layers[0]?.id||'');setTab('results');setDrawerOpen(true);refresh();}
        if(['queued','running'].includes(next.state))timer=setTimeout(poll,1000);else{refresh();if(next.state==='failed'){setError(next.message);setDrawerOpen(true);}}
      }catch(e){if(!stopped){setError((e as Error).message);timer=setTimeout(poll,3000);}}
    };
    timer=setTimeout(poll,300);return()=>{stopped=true;clearTimeout(timer);};
  },[job?.id,active]);

  function chooseModel(id:Hazard){
    const selected=models.find(m=>m.id===id)!;setHazard(id);setParameters(Object.fromEntries(selected.fields.map(f=>[f.key,f.default])));
    setBuffer(selected.buffer);setIgnitions([]);setMode(null);setJob(null);setError('');
  }
  function draw(next:'area'|'ignition'){setMode(mode===next?null:next);setDrawerOpen(false);}
  function command(type:MapAction['type']){setAction(previous=>({id:(previous?.id||0)+1,type}));}
  function showResults(){setTab('results');setDrawerOpen(true);}
  function clearArea(){setBbox(null);setIgnitions([]);setMode(null);setJob(null);setLayerId('');setTab('settings');}
  async function fileUpload(key:string,file?:File){
    if(!file)return;setError('');setUploading(key);
    try{const data=new FormData();data.append('file',file);const value=await api<{id:string;name:string}>('/uploads',{method:'POST',body:data});setAssets(old=>({...old,[key]:value}));}
    catch(e){setError((e as Error).message);}finally{setUploading('');}
  }
  async function calculate(){
    if(!bbox){setError('Выделите участок на карте.');return;}setError('');setBusy(true);
    try{
      const needed=hazard==='wildfire'?['dem','rail','fuel','weather']:hazard==='landslide'?['dem','rail','soil']:['dem','rail','release'];
      const payload:Record<string,unknown>={hazard,bbox,parameters,buffer,resolution,scenario_date:date,satellite,ignitions,
        corridor:Number(extra.corridor),...Object.fromEntries(needed.filter(k=>assets[k]).map(k=>[k+'_id',assets[k].id]))};
      if(hazard==='avalanche'){
        for(const key of ['release_min_slope','release_max_slope','release_min_relief','release_min_area'])payload[key]=Number(extra[key as keyof typeof extra]);
        if(extra.frequency!==''){payload.frequency=Number(extra.frequency);payload.vulnerability=Number(extra.vulnerability);payload.repair_cost=Number(extra.repair_cost);}
      }
      if(hazard==='wildfire'){payload.lookback_days=Number(extra.lookback_days);payload.warmup_days=Number(extra.warmup_days);}
      setJob(await api<Job>('/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));setLayerId('');refresh();
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  function go(){
    const parts=coordinate.split(/[,;\s]+/).filter(Boolean).map(Number);
    if(parts.length!==2||!parts.every(Number.isFinite)||Math.abs(parts[0])>180||Math.abs(parts[1])>80){setError('Введите долготу и широту, например: 94.3, 51.1');return;}
    setFocus([parts[0]-.04,parts[1]-.025,parts[0]+.04,parts[1]+.025]);
  }
  async function openJob(record:Job){
    try{
      const selected=await api<Job>('/jobs/'+record.id);
      setJob(selected);setLayerId(selected.result?.layers[0]?.id||'');setFocus([...selected.bbox]);setBbox([...selected.bbox]);setHazard(selected.hazard);setMode(null);setError('');
      setCoordinate(`${((selected.bbox[0]+selected.bbox[2])/2).toFixed(4)}, ${((selected.bbox[1]+selected.bbox[3])/2).toFixed(4)}`);
      const r=selected.request;
      if(r){
        setParameters(r.parameters);setBuffer(r.buffer??models.find(m=>m.id===selected.hazard)!.buffer);setResolution(r.resolution);setDate(r.scenario_date);setSatellite(r.satellite);setIgnitions(r.ignitions);
        setExtra(Object.fromEntries(Object.keys(initialExtra).map(k=>[k,r[k]===null?'':String(r[k]??initialExtra[k as keyof typeof initialExtra])])) as typeof initialExtra);
        setAssets(Object.fromEntries(['dem','rail','soil','fuel','release','weather'].filter(k=>r[k+'_id']).map(k=>[k,{id:String(r[k+'_id']),name:'Файл сохранённого расчёта · '+k}])));
      }
      setTab(selected.result?'results':'settings');
    }catch(e){setError((e as Error).message);}
  }

  const layer=result?.layers.find(l=>l.id===layerId)||null;
  const fileField=(key:string,label:string,accept:string)=><div className="file-row" key={key}>
    <label className="file-label"><Upload size={15}/><span>{assets[key]?.name||label}</span><input disabled={!!uploading||active} type="file" accept={accept} onChange={e=>{fileUpload(key,e.target.files?.[0]);e.target.value='';}}/>
      {uploading===key?<LoaderCircle className="spin" size={15}/>:assets[key]?<Check size={15}/>:null}</label>
    {assets[key]&&<button className="icon-button" aria-label={'Убрать '+label} onClick={()=>setAssets(old=>{const next={...old};delete next[key];return next;})}><X size={14}/></button>}
  </div>;
  const parameterFields=(advanced:boolean)=>model?.fields.filter(f=>MAIN[hazard].includes(f.key)!==advanced).map(f=><label className={'field '+(f.type==='select'?'full':'')} key={f.key}>
    <span>{f.label}{f.unit&&<small>{f.unit}</small>}</span>{f.type==='select'?<select value={parameters[f.key]??f.default} onChange={e=>setParameters(p=>({...p,[f.key]:e.target.value}))}>
      {Object.entries(f.options||{}).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select>:<input type="number" step="any" required value={parameters[f.key]??''} onChange={e=>setParameters(p=>({...p,[f.key]:e.target.value===''?'':Number(e.target.value)}))}/>}</label>);
  const extraField=(key:keyof typeof extra,label:string)=><label className="field" key={key}><span>{label}</span><input type="number" step="any" value={extra[key]} onChange={e=>setExtra({...extra,[key]:e.target.value})}/></label>;
  const tool=(label:string,icon:React.ReactNode,onClick:()=>void,selected=false,disabled=false)=><button type="button" aria-label={label} title={label} className={selected?'selected':''} onClick={onClick} disabled={disabled}>{icon}</button>;

  return <div className="app-shell"><div className="workspace">
    <main className="map-workspace"><MapView bbox={bbox} onArea={b=>{setBbox(b);setIgnitions([]);setJob(null);setLayerId('');setTab('settings');}} mode={mode} onDone={()=>setMode(null)} ignitions={hazard==='wildfire'?ignitions:[]} onIgnition={p=>setIgnitions(old=>[...old,p])} layer={layer} opacity={opacity} focus={focus} action={action} drawerOpen={drawerOpen} onError={message=>{setError(message);setDrawerOpen(true);}}/>
      <header className="map-header"><button className="menu-button" aria-label={drawerOpen?'Закрыть панель инструментов':'Открыть панель инструментов'} aria-expanded={drawerOpen} onClick={()=>setDrawerOpen(!drawerOpen)}>{drawerOpen?<X size={21}/>:<Menu size={21}/>}</button><span className="brand">ГеоРиск</span></header>
      {active&&job&&<button className="job-indicator" onClick={()=>{setTab('settings');setDrawerOpen(true);}}><LoaderCircle className="spin" size={15}/>{job.progress}%<span>{job.message}</span></button>}
      {mode&&<div className="map-instruction"><span>{mode==='area'?'Укажите два противоположных угла участка':'Укажите точку зажигания'}</span><button aria-label="Отменить действие" onClick={()=>setMode(null)}><X size={16}/></button></div>}
      <nav className="map-toolbar" aria-label="Инструменты карты">
        {tool('Перемещение карты',<Hand size={19}/>,()=>setMode(null),!mode)}
        {tool('Выделить участок',<SquareDashedMousePointer size={19}/>,()=>draw('area'),mode==='area',active)}
        {hazard==='wildfire'&&tool('Указать очаг пожара',<HazardIcon hazard="wildfire" size={24}/>,()=>draw('ignition'),mode==='ignition',active||!bbox)}
        <span className="toolbar-divider"/>
        {tool('Приблизить',<Plus size={20}/>,()=>command('zoomIn'))}
        {tool('Отдалить',<Minus size={20}/>,()=>command('zoomOut'))}
        {tool('Показать участок',<LocateFixed size={19}/>,()=>bbox&&setFocus([...bbox]),false,!bbox)}
        {tool('Север сверху',<Compass size={19}/>,()=>command('north'))}
        <span className="toolbar-divider"/>
        {tool('Слои и результаты',<Layers size={19}/>,showResults,drawerOpen&&tab==='results',!result)}
        {tool('Очистить участок',<Trash2 size={18}/>,clearArea,false,active||!bbox)}
        {tool('Полный экран',<Maximize size={18}/>,()=>command('fullscreen'))}
      </nav>
      {drawerOpen&&<button className="drawer-scrim" aria-label="Закрыть меню" onClick={()=>setDrawerOpen(false)}/>}
    </main>
    <aside className={'sidebar'+(drawerOpen?' open':'')} aria-label="Панель инструментов" aria-hidden={!drawerOpen}>
      <div className="side-tabs">
        <button className={tab==='settings'?'chosen':''} onClick={()=>setTab('settings')}><SlidersHorizontal size={15}/>Расчёт</button>
        <button className={tab==='results'?'chosen':''} disabled={!result} onClick={()=>setTab('results')}><Layers size={15}/>Результат</button>
        <button className={tab==='history'?'chosen':''} onClick={()=>{setTab('history');refresh();}}><History size={15}/>История</button>
      </div>
      <div className="side-content">
        {tab==='results'&&result?<><h1 className="panel-title">{NAMES[job!.hazard]}</h1><ResultPanel result={result} layerId={layerId} onLayer={setLayerId} opacity={opacity} onOpacity={setOpacity}/></>:tab==='history'?<>
          <h1 className="panel-title">История расчётов</h1>{!jobs.length&&<p className="hint">Пока нет сохранённых расчётов.</p>}
          {jobs.map(item=><button className="history-card" key={item.id} onClick={()=>openJob(item)}><span className={'history-icon '+item.hazard}><HazardIcon hazard={item.hazard} size={26}/></span><span><strong>{NAMES[item.hazard]}</strong><small>{new Date(item.created_at).toLocaleString('ru-RU')}</small><em>{STATUS[item.state]||item.state}</em></span><ChevronRight size={15}/></button>)}
        </>:<>
          <h1 className="panel-title">Модель</h1><div className="model-buttons">{models.map(m=>{return <button disabled={active} key={m.id} className={'model-button '+m.id+(hazard===m.id?' selected':'')} onClick={()=>chooseModel(m.id)}><HazardIcon hazard={m.id} size={36}/><span>{m.title}</span></button>;})}</div>
          <h2 className="section-heading">Участок</h2>
          <div className="search-coordinate"><MapPin size={16}/><input aria-label="Долгота, широта" value={coordinate} onChange={e=>setCoordinate(e.target.value)} onKeyDown={e=>e.key==='Enter'&&go()} placeholder="Долгота, широта"/><button aria-label="Перейти к координатам" onClick={go}><ArrowUpRight size={17}/></button></div>
          <button className="draw-button" onClick={()=>draw('area')} disabled={active}><SquareDashedMousePointer size={17}/>Выделить участок<ChevronRight size={15}/></button>
          {bbox?<div className="area-info"><span>{bbox.map(v=>v.toFixed(4)).join(' · ')}</span><button aria-label="Приблизить к участку" onClick={()=>setFocus([...bbox])}><LocateFixed size={15}/></button></div>:<p className="hint">Два клика по карте — противоположные углы.</p>}
          <div className="fields-grid compact"><label className="field"><span>Буфер анализа <small>м</small></span><input type="number" min="0" max="20000" value={buffer} onChange={e=>setBuffer(Number(e.target.value))}/></label><label className="field"><span>Шаг сетки <small>м</small></span><input type="number" min="1" max="1000" value={resolution} onChange={e=>setResolution(Number(e.target.value))}/></label></div>
          <h2 className="section-heading">Параметры</h2><div className="fields-grid">{parameterFields(false)}</div>
          {hazard==='wildfire'&&<><label className="field date-field"><span>Дата сценария</span><input type="date" value={date} onChange={e=>setDate(e.target.value)}/></label><label className="toggle"><input type="checkbox" checked={satellite} onChange={e=>setSatellite(e.target.checked)}/><span>Sentinel-2 · NDVI / NDMI</span></label><button className="draw-button" disabled={!bbox||active} onClick={()=>draw('ignition')}><HazardIcon hazard="wildfire" size={22}/>Указать очаг<span className="count">{ignitions.length}</span></button>{!!ignitions.length&&<button className="text-button" onClick={()=>setIgnitions([])}>Убрать очаги</button>}</>}
          <details className="details"><summary>Дополнительные параметры<ChevronDown size={16}/></summary><div className="fields-grid">{parameterFields(true)}{extraField('corridor','Коридор вдоль оси ЖД, м')}{hazard==='wildfire'&&<>{extraField('lookback_days','Поиск снимков, дней')}{extraField('warmup_days','Прогрев FWI, дней')}</>}{hazard==='avalanche'&&<>{extraField('release_min_slope','Мин. уклон очага, °')}{extraField('release_max_slope','Макс. уклон очага, °')}{extraField('release_min_relief','Мин. перепад высот, м')}{extraField('release_min_area','Мин. площадь очага, м²')}</>}</div></details>
          <details className="details"><summary>Свои данные и трасса ЖД<ChevronDown size={16}/></summary><p className="hint">GeoTIFF высот; геометрия — GeoJSON WGS 84.</p>{fileField('dem','Модель рельефа','.tif,.tiff')}{fileField('rail','Ось железной дороги','.geojson')}{hazard==='landslide'&&<>{fileField('soil','Зоны свойств грунта','.geojson')}<p className="hint code-hint">Поля: c_kpa, root_kpa, phi_deg, gamma_knm3, depth_m, m_ratio.</p></>}{hazard==='avalanche'&&fileField('release','Лавинные очаги','.geojson')}{hazard==='wildfire'&&<>{fileField('fuel','Типы горючего','.geojson')}{fileField('weather','Метеоряд','.csv')}<p className="hint code-hint">Горючее: поле fuel. CSV: date, temp_c, rh_pct, wind_kmh, wind_from_deg, rain_mm.</p></>}</details>
          {hazard==='avalanche'&&<details className="details"><summary>Денежный риск для ЖД<ChevronDown size={16}/></summary><div className="fields-grid">{extraField('frequency','Частота сценария, в год')}{extraField('vulnerability','Доля ущерба, 0–1')}{extraField('repair_cost','Восстановление, руб./м')}</div></details>}
          <details className="details"><summary>Метод и исходные данные<ChevronDown size={16}/></summary><p className="hint">{model?.method}</p><p className="hint">{model?.note}</p>{hazard==='landslide'&&<p className="hint">Обводнение 0,5: вода занимает нижнюю половину слоя. Значения грунта по умолчанию — сценарные допущения.</p>}<a className="source-link" href="/docs" target="_blank" rel="noreferrer">Документация API<ArrowUpRight size={13}/></a></details>
        </>}
      </div>
      <div className="side-footer">
        {error&&<div className="error" role="alert"><span>{error}</span><button aria-label="Закрыть ошибку" onClick={()=>setError('')}><X size={15}/></button></div>}
        {active&&job?<><div className="progress-label"><span>{job.message}</span><strong>{job.progress}%</strong></div><div className="progress-track"><div style={{width:job.progress+'%'}}/></div><button className="cancel-button" onClick={()=>api('/jobs/'+job.id+'/cancel',{method:'POST'}).catch(e=>setError(e.message))}><X size={15}/>Отменить</button></>:<button className="calculate" disabled={busy||!!uploading||!models.length} onClick={calculate}>{busy?<LoaderCircle className="spin" size={18}/>:<Play size={17}/>}Рассчитать<ArrowUpRight size={18}/></button>}
      </div>
    </aside>
  </div></div>;
}
