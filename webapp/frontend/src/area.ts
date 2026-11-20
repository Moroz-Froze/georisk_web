import type {Bbox,Hazard} from './types';

export type SavedArea={id:string;bbox:Bbox;created_at:string;job_id?:string};
export type AreaOptions={hazard:Hazard;buffer:number;resolution:number};
export type AreaCheck={valid:boolean;message:string;area_km2:number;cells:number;max_cells:number;max_area_km2:number};
export const AREA_STORAGE='georisk.areas.v1';

/** Immediate feedback during dragging; the server checks the exact projected grid on release. */
export function estimateArea(bbox:Bbox,options:AreaOptions):AreaCheck{
  const [west,south,east,north]=bbox,rad=Math.PI/180,radius=6371008.8;
  const width=radius*(east-west)*rad*Math.cos((south+north)/2*rad);
  const height=radius*(north-south)*rad;
  const area_km2=radius**2*(east-west)*rad*(Math.sin(north*rad)-Math.sin(south*rad))/1e6;
  const cells=Math.ceil((width+2*options.buffer)/options.resolution)*Math.ceil((height+2*options.buffer)/options.resolution);
  const max_cells=options.hazard==='wildfire'?300000:2000000;
  let message='';
  if(!bbox.every(Number.isFinite)||west < -180||east > 180||west>=east||south>=north||south < -80||north > 80||east-west>6){
    message='Выделите участок в пределах широт от −80 до 80° и шириной не более 6°.';
  }else if(!Number.isFinite(options.buffer)||options.buffer<0||!Number.isFinite(options.resolution)||options.resolution<=0){
    message='Проверьте буфер и шаг сетки.';
  }else if(area_km2>2500){
    message='Площадь участка превышает 2500 км². Выделите меньший участок.';
  }else if(cells>max_cells){
    message='Участок слишком большой для выбранной сетки. Уменьшите участок или буфер либо увеличьте шаг сетки.';
  }
  return {valid:!message,message,area_km2,cells,max_cells,max_area_km2:2500};
}

export function loadAreas(storage:Pick<Storage,'getItem'>,key=AREA_STORAGE):SavedArea[]{
  try{
    const value:unknown=JSON.parse(storage.getItem(key)||'[]');
    if(!Array.isArray(value))return [];
    const ids=new Set<string>();
    return value.filter((item):item is SavedArea=>{
      if(!item||typeof item.id!=='string'||ids.has(item.id)||typeof item.created_at!=='string'||
        !Array.isArray(item.bbox)||item.bbox.length!==4||!item.bbox.every((v:unknown)=>typeof v==='number'&&Number.isFinite(v))||
        item.bbox[0]>=item.bbox[2]||item.bbox[1]>=item.bbox[3]||item.bbox[0]<-180||item.bbox[2]>180||item.bbox[1]<-86||item.bbox[3]>86)return false;
      ids.add(item.id);return true;
    }).map(item=>({id:item.id,bbox:item.bbox,created_at:item.created_at,...(typeof item.job_id==='string'?{job_id:item.job_id}:{})}));
  }catch{return [];}
}

export function areaLabel(area:number):string{
  return Number.isFinite(area)?area.toLocaleString('ru-RU',{maximumFractionDigits:2})+' км²':'—';
}
