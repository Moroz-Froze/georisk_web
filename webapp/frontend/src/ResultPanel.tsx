import {Layers,Download,FileText,ChevronDown,ArrowUpRight} from 'lucide-react';
import type {Result} from './types';

const METRICS:Record<string,[string,string]>={min_fs:['Минимальный FS',''],target_unstable_area_m2:['Неустойчивая площадь','га'],target_low_reserve_area_m2:['Малый запас','га'],
  max_speed_m_s:['Макс. скорость','м/с'],max_pressure_kpa:['Макс. давление','кПа'],fwi:['Индекс FWI',''],max_ros_m_min:['Скорость фронта','м/мин'],scenario_area_m2:['Площадь сценария','га']};
const RAIL_LABELS:Record<string,string>={affected_length_m:'Длина под воздействием, м',unstable_length_m:'Длина в неустойчивых зонах, м',low_reserve_length_m:'Длина с малым запасом, м',hit_length_m:'Длина в зоне огня, м',expected_annual_loss:'Годовой ущерб, руб./год',incomplete:'Объекты с неполным покрытием'};
function format(value:number,unit='',digits=2){return (unit==='га'?value/10000:value).toLocaleString('ru-RU',{maximumFractionDigits:digits});}
type Props={result:Result;layerId:string;onLayer:(id:string)=>void;opacity:number;onOpacity:(value:number)=>void};

export default function ResultPanel({result,layerId,onLayer,opacity,onOpacity}:Props){
  const layer=result.layers.find(l=>l.id===layerId);
  return <section className="result-panel">
    {['terrain_not_suitable','no_analyzable_slopes'].includes(result.status)&&<div className="result-warning">Участок не прошёл критерии модели. Проверьте рельеф, область и пороги. Это не заключение о безопасности.</div>}
    <div className="metrics">{Object.entries(result.summary).filter(([k,v])=>k in METRICS&&typeof v==='number').map(([key,value])=>{
      const[label,unit]=METRICS[key];return <div className="metric" key={key}><span>{label}</span><strong>{format(value as number,unit,key==='min_fs'?4:2)}<small>{unit}</small></strong></div>;
    })}</div>
    <label className="field layer-select"><span><Layers size={14}/>Слой на карте</span><select value={layerId} onChange={e=>onLayer(e.target.value)}>
      <option value="">Скрыть результаты</option>{result.layers.map(l=><option key={l.id} value={l.id}>{l.title}</option>)}
    </select></label>
    {layer&&<><div className="legend">{layer.legend.map((item,i)=><span key={i}><i style={{background:item.color}}/>{item.label}</span>)}</div>
      <label className="opacity">Непрозрачность<input aria-label="Непрозрачность слоя" type="range" min="0" max="1" step=".05" value={opacity} onChange={e=>onOpacity(Number(e.target.value))}/></label></>}
    <div className="result-actions"><a href={result.archive_url}><Download size={16}/>Скачать ZIP</a><a href={result.report_url} target="_blank" rel="noreferrer"><FileText size={16}/>Отчёт</a></div>
    {!!result.railway&&<details className="details"><summary>Железная дорога<ChevronDown size={15}/></summary>{Object.entries(result.railway).filter(([k])=>k in RAIL_LABELS).map(([key,value])=><p className="rail-row" key={key}><span>{RAIL_LABELS[key]}</span><b>{value===null?'Не определено':typeof value==='number'?format(value):value}</b></p>)}</details>}
    <details className="details"><summary>Исходные данные и ограничения<ChevronDown size={15}/></summary>
      {result.provenance?.scenario_date&&<p className="warning-item">Дата сценария: {result.provenance.scenario_date}</p>}
      {result.provenance?.satellite&&<p className="warning-item">Снимок: {result.provenance.satellite.date}. Безоблачное покрытие: {format(result.provenance.satellite.clear_coverage*100)}%.</p>}
      {result.warnings.map((warning,i)=><p className="warning-item" key={i}>{warning}</p>)}
      {result.sources.map(s=><a className="source-link" key={s.url} href={s.url} target="_blank" rel="noreferrer">{s.title}<ArrowUpRight size={13}/></a>)}
    </details>
    <details className="details"><summary>Отдельные файлы<ChevronDown size={15}/></summary>{result.files.map(file=><a className="source-link" key={file.id} href={file.url}><span>{file.name}</span><Download size={13}/></a>)}</details>
  </section>;
}
