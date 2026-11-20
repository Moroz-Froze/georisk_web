import {useEffect,useRef,useState} from 'react';
import {LoaderCircle,Play,Trash2} from 'lucide-react';
import * as maplibregl from 'maplibre-gl';
import {type GeoJSONSource,type Map as LibreMap,type MapMouseEvent} from 'maplibre-gl';
import type {FeatureCollection,Polygon} from 'geojson';
import type {Bbox,Layer} from './types';
import {areaLabel,estimateArea,type AreaCheck,type AreaOptions,type SavedArea} from './area';
import 'maplibre-gl/dist/maplibre-gl.css';
import mapWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';

maplibregl.setWorkerUrl(mapWorkerUrl);

export type MapAction={id:number;type:'zoomIn'|'zoomOut'|'north'|'fullscreen'};
type Props={bbox:Bbox|null;areas:SavedArea[];selectedId:string|null;options:AreaOptions;
  check:AreaCheck|null;checking:boolean;calculateDisabled:boolean;active:boolean;
  onCalculate:()=>void;onRemove:(id:string)=>void;onSelect:(area:SavedArea)=>void;
  onArea:(bbox:Bbox)=>void;mode:'area'|'ignition'|null;onDone:()=>void;
  ignitions:[number,number][];onIgnition:(point:[number,number])=>void;layer:Layer|null;opacity:number;focus:Bbox|null;
  action:MapAction|null;drawerOpen:boolean;onError:(message:string)=>void};
const empty:FeatureCollection={type:'FeatureCollection',features:[]};
function rectangle(bbox:Bbox,properties:Record<string,unknown>={}):FeatureCollection<Polygon>{
  const [w,s,e,n]=bbox;
  return {type:'FeatureCollection',features:[{type:'Feature',properties,geometry:{type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]}}]};
}
function bounds(a:[number,number],b:[number,number]):Bbox{return [Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];}
function data(map:LibreMap,id:string,value:FeatureCollection){(map.getSource(id) as GeoJSONSource|undefined)?.setData(value);}
type Drag={id:number;start:[number,number];x:number;y:number};

export default function MapView(props:Props){
  const container=useRef<HTMLDivElement>(null),map=useRef<LibreMap|null>(null),drag=useRef<Drag|null>(null),lastDraw=useRef(0);
  const [ready,setReady]=useState(false),[draft,setDraft]=useState<Bbox|null>(null),[anchor,setAnchor]=useState({x:12,y:90});
  const current=useRef(props);current.current=props;
  useEffect(()=>{
    let m:LibreMap;
    try{m=new maplibregl.Map({container:container.current!,center:[94.3,51.1],zoom:7.3,minZoom:2,maxZoom:19,
      attributionControl:false,renderWorldCopies:false,
      style:{version:8,sources:{osm:{type:'raster',tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,
        maxzoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>'}},layers:[{id:'basemap',type:'raster',source:'osm'}]}});
    }catch{current.current.onError('Не удалось запустить карту MapLibre. Проверьте поддержку WebGL в браузере.');return;}
    map.current=m;
    m.addControl(new maplibregl.AttributionControl({compact:true}),'bottom-right');
    m.addControl(new maplibregl.ScaleControl({maxWidth:100,unit:'metric'}),'bottom-left');
    m.on('load',()=>{
      const accent=getComputedStyle(container.current!).getPropertyValue('--accent').trim();
      for(const id of ['area','preview','ignitions'])m.addSource(id,{type:'geojson',data:empty});
      m.addLayer({id:'area-fill',type:'fill',source:'area',paint:{
        'fill-color':['case',['get','invalid'],'#dc2626',['get','active'],accent,'#7992ad'],
        'fill-opacity':['case',['get','invalid'],.16,['get','active'],.1,.035]}});
      m.addLayer({id:'area-line',type:'line',source:'area',paint:{
        'line-color':['case',['get','invalid'],'#dc2626',['get','active'],accent,'#7992ad'],
        'line-width':['case',['get','active'],2.5,1.5]}});
      m.addLayer({id:'preview-fill',type:'fill',source:'preview',paint:{'fill-color':['case',['get','invalid'],'#dc2626',accent],'fill-opacity':.14}});
      m.addLayer({id:'preview-line',type:'line',source:'preview',paint:{'line-color':['case',['get','invalid'],'#dc2626',accent],'line-width':2,'line-dasharray':[3,2]}});
      m.addLayer({id:'ignition-points',type:'circle',source:'ignitions',paint:{'circle-radius':7,'circle-color':'#e16637','circle-stroke-color':'#fff','circle-stroke-width':2}});
      setReady(true);
    });
    const canvas=m.getCanvas();
    const point=(event:PointerEvent):[number,number]=>{
      const box=canvas.getBoundingClientRect();
      const position=m.unproject([Math.max(0,Math.min(box.width,event.clientX-box.left)),Math.max(0,Math.min(box.height,event.clientY-box.top))]);
      return [position.lng,position.lat];
    };
    const reset=()=>{
      const previous=drag.current;drag.current=null;
      if(previous&&canvas.hasPointerCapture(previous.id))canvas.releasePointerCapture(previous.id);
      setDraft(null);data(m,'preview',empty);
    };
    const down=(event:PointerEvent)=>{
      if(event.button!==0||!event.isPrimary||current.current.mode!=='area'||!m.getSource('preview'))return;
      event.preventDefault();event.stopPropagation();
      drag.current={id:event.pointerId,start:point(event),x:event.clientX,y:event.clientY};
      canvas.setPointerCapture(event.pointerId);
    };
    const move=(event:PointerEvent)=>{
      if(!drag.current||event.pointerId!==drag.current.id)return;
      event.preventDefault();event.stopPropagation();setDraft(bounds(drag.current.start,point(event)));
    };
    const up=(event:PointerEvent)=>{
      const previous=drag.current;if(!previous||previous.id!==event.pointerId)return;
      event.preventDefault();event.stopPropagation();
      const box=bounds(previous.start,point(event)),distance=Math.hypot(event.clientX-previous.x,event.clientY-previous.y);
      reset();lastDraw.current=performance.now();
      if(distance<5||box[2]-box[0]<1e-7||box[3]-box[1]<1e-7)return;
      current.current.onArea(box);current.current.onDone();
    };
    const cancel=()=>{if(drag.current){reset();current.current.onDone();}};
    canvas.addEventListener('pointerdown',down,true);canvas.addEventListener('pointermove',move,true);canvas.addEventListener('pointerup',up,true);
    canvas.addEventListener('pointercancel',cancel);canvas.addEventListener('lostpointercapture',cancel);
    m.on('click',(event:MapMouseEvent)=>{
      const p=current.current;if(performance.now()-lastDraw.current<250)return;
      if(p.mode==='ignition'){p.onIgnition([event.lngLat.lng,event.lngLat.lat]);p.onDone();return;}
      if(p.mode||p.active||!m.getLayer('area-fill'))return;
      const feature=m.queryRenderedFeatures(event.point,{layers:['area-fill']})[0];
      const area=p.areas.find(item=>item.id===feature?.properties?.id);if(area)p.onSelect(area);
    });
    m.on('mousemove',event=>{
      if(!current.current.mode&&m.getLayer('area-fill'))canvas.style.cursor=m.queryRenderedFeatures(event.point,{layers:['area-fill']}).length?'pointer':'';
    });
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'){reset();current.current.onDone();}};
    window.addEventListener('keydown',escape);window.addEventListener('blur',cancel);
    const observer=new ResizeObserver(()=>m.resize());observer.observe(container.current!);
    return()=>{
      window.removeEventListener('keydown',escape);window.removeEventListener('blur',cancel);observer.disconnect();
      canvas.removeEventListener('pointerdown',down,true);canvas.removeEventListener('pointermove',move,true);canvas.removeEventListener('pointerup',up,true);
      canvas.removeEventListener('pointercancel',cancel);canvas.removeEventListener('lostpointercapture',cancel);
      m.remove();map.current=null;setReady(false);
    };
  },[]);
  useEffect(()=>{
    if(!ready||!map.current)return;const m=map.current,canvas=m.getCanvas(),previous=drag.current;drag.current=null;
    if(previous&&canvas.hasPointerCapture(previous.id))canvas.releasePointerCapture(previous.id);
    setDraft(null);data(m,'preview',empty);canvas.style.cursor=props.mode?'crosshair':'';
    canvas.style.touchAction=props.mode==='area'?'none':'';
    if(props.mode==='area'){m.dragPan.disable();m.boxZoom.disable();m.touchZoomRotate.disable();}
    else{m.dragPan.enable();m.boxZoom.enable();m.touchZoomRotate.enable();}
    if(props.mode)m.doubleClickZoom.disable();else m.doubleClickZoom.enable();
  },[ready,props.mode]);
  useEffect(()=>{
    if(!ready||!map.current)return;
    const features=[...props.areas].sort((a,b)=>Number(a.id===props.selectedId)-Number(b.id===props.selectedId)).flatMap(area=>{
      const active=area.id===props.selectedId,check=active&&props.check?props.check:estimateArea(area.bbox,props.options);
      return rectangle(area.bbox,{id:area.id,active,invalid:!check.valid}).features;
    });
    data(map.current,'area',{type:'FeatureCollection',features});
  },[ready,props.areas,props.selectedId,props.check,props.options.hazard,props.options.buffer,props.options.resolution]);
  useEffect(()=>{
    if(ready&&map.current)data(map.current,'preview',draft?rectangle(draft,{invalid:!estimateArea(draft,props.options).valid}):empty);
  },[ready,draft,props.options.hazard,props.options.buffer,props.options.resolution]);
  useEffect(()=>{
    if(!ready||!map.current||!props.bbox)return;const m=map.current,bbox=props.bbox;
    const position=()=>{
      const [w,,e,n]=bbox,right=m.project([e,n]),left=m.project([w,n]),width=m.getContainer().clientWidth,height=m.getContainer().clientHeight;
      const minimum=props.drawerOpen&&width>700?392:12,cardWidth=Math.min(250,width-24);
      const x=right.x+12+cardWidth<=width?right.x+12:left.x-cardWidth-12;
      setAnchor({x:Math.max(minimum,Math.min(width-cardWidth-12,x)),y:Math.max(82,Math.min(height-250,right.y))});
    };
    position();m.on('move',position);m.on('resize',position);
    return()=>{m.off('move',position);m.off('resize',position);};
  },[ready,props.bbox,props.drawerOpen]);
  useEffect(()=>{
    if(!ready||!map.current)return;const m=map.current;
    if(m.getLayer('result'))m.removeLayer('result');if(m.getSource('result-image'))m.removeSource('result-image');if(!props.layer)return;
    const [[s,w],[n,e]]=props.layer.bounds;
    m.addSource('result-image',{type:'image',url:props.layer.url,coordinates:[[w,n],[e,n],[e,s],[w,s]]});
    m.addLayer({id:'result',type:'raster',source:'result-image',paint:{'raster-opacity':props.opacity,'raster-fade-duration':0,'raster-resampling':'nearest'}},'area-fill');
  },[ready,props.layer]);
  useEffect(()=>{if(ready&&map.current?.getLayer('result'))map.current.setPaintProperty('result','raster-opacity',props.opacity);},[ready,props.opacity]);
  useEffect(()=>{if(!ready||!map.current)return;data(map.current,'ignitions',{type:'FeatureCollection',features:props.ignitions.map(coordinates=>({type:'Feature',properties:{},geometry:{type:'Point',coordinates}}))});},[ready,props.ignitions]);
  useEffect(()=>{if(ready&&map.current&&props.focus){const[w,s,e,n]=props.focus;const left=props.drawerOpen&&map.current.getContainer().clientWidth>700?400:50;map.current.fitBounds([[w,s],[e,n]],{padding:{left,right:50,top:90,bottom:110},duration:500});}},[ready,props.focus,props.drawerOpen]);
  useEffect(()=>{
    const m=map.current;if(!ready||!m||!props.action)return;
    switch(props.action.type){case 'zoomIn':m.zoomIn({duration:220});break;case 'zoomOut':m.zoomOut({duration:220});break;case 'north':m.easeTo({bearing:0,pitch:0,duration:300});break;
      case 'fullscreen':if(document.fullscreenElement)void document.exitFullscreen();else void container.current?.closest<HTMLElement>('.app-shell')?.requestFullscreen().catch(()=>current.current.onError('Полноэкранный режим недоступен в этом браузере.'));break;}
  },[ready,props.action]);
  const draftCheck=draft?estimateArea(draft,props.options):null;
  return <><div className="map" ref={container} aria-label="Карта участка обследования"/>
    {draftCheck&&<div className={'drawing-size'+(!draftCheck.valid?' invalid':'')} role="status">≈ {areaLabel(draftCheck.area_km2)}{!draftCheck.valid&&<span>Участок слишком большой или недопустимый</span>}</div>}
    {ready&&props.bbox&&props.selectedId&&!props.mode&&<section className={'area-actions'+(props.check&&!props.check.valid?' invalid':'')+(props.drawerOpen?' drawer-visible':'')}
      aria-label="Действия с участком" style={{left:anchor.x,top:anchor.y}}>
      <div className="area-actions-heading"><strong>{props.check&&!props.check.valid?'Участок недоступен для расчёта':'Выделенный участок'}</strong>
        <button type="button" className="remove-area" aria-label="Удалить участок с карты" title="Удалить участок с карты" disabled={props.active} onClick={()=>props.onRemove(props.selectedId!)}><Trash2 size={16}/></button></div>
      <span className="area-size">{areaLabel(props.check?.area_km2??estimateArea(props.bbox,props.options).area_km2)}</span>
      {props.check&&!props.check.valid&&<p className="area-limit-message" role="status">{props.check.message}</p>}
      <button type="button" className="area-calculate" disabled={props.calculateDisabled||props.checking||!props.check?.valid} onClick={props.onCalculate}>
        {props.checking||props.active?<LoaderCircle className="spin" size={16}/>:<Play size={16}/>}{props.checking?'Проверка участка…':props.active?'Расчёт выполняется':'Рассчитать'}</button>
    </section>}
  </>;
}
