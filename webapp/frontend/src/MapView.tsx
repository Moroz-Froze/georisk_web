import {useEffect,useRef,useState} from 'react';
import maplibregl, {type GeoJSONSource,type Map as LibreMap,type MapMouseEvent} from 'maplibre-gl';
import type {FeatureCollection,Polygon} from 'geojson';
import type {Bbox,Layer} from './types';
import 'maplibre-gl/dist/maplibre-gl.css';

export type MapAction={id:number;type:'zoomIn'|'zoomOut'|'north'|'fullscreen'};
type Props={bbox:Bbox|null;onArea:(bbox:Bbox)=>void;mode:'area'|'ignition'|null;onDone:()=>void;
  ignitions:[number,number][];onIgnition:(point:[number,number])=>void;layer:Layer|null;opacity:number;focus:Bbox|null;
  action:MapAction|null;drawerOpen:boolean;onError:(message:string)=>void};
const empty:FeatureCollection={type:'FeatureCollection',features:[]};
function rectangle(bbox:Bbox|null):FeatureCollection<Polygon>{
  if(!bbox)return {type:'FeatureCollection',features:[]};
  const [w,s,e,n]=bbox;
  return {type:'FeatureCollection',features:[{type:'Feature',properties:{},geometry:{type:'Polygon',coordinates:[[[w,s],[e,s],[e,n],[w,n],[w,s]]]}}]};
}
function bounds(a:[number,number],b:[number,number]):Bbox{return [Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];}
function data(map:LibreMap,id:string,value:FeatureCollection){(map.getSource(id) as GeoJSONSource|undefined)?.setData(value);}

export default function MapView(props:Props){
  const container=useRef<HTMLDivElement>(null),map=useRef<LibreMap|null>(null),first=useRef<[number,number]|null>(null);
  const [ready,setReady]=useState(false);
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
      m.addLayer({id:'area-fill',type:'fill',source:'area',paint:{'fill-color':accent,'fill-opacity':.06}});
      m.addLayer({id:'area-line',type:'line',source:'area',paint:{'line-color':accent,'line-width':2}});
      m.addLayer({id:'preview-fill',type:'fill',source:'preview',paint:{'fill-color':accent,'fill-opacity':.08}});
      m.addLayer({id:'preview-line',type:'line',source:'preview',paint:{'line-color':accent,'line-width':2,'line-dasharray':[3,2]}});
      m.addLayer({id:'ignition-points',type:'circle',source:'ignitions',paint:{'circle-radius':7,'circle-color':'#e16637','circle-stroke-color':'#fff','circle-stroke-width':2}});
      setReady(true);
    });
    m.on('click',(event:MapMouseEvent)=>{
      const p=current.current,point:[number,number]=[event.lngLat.lng,event.lngLat.lat];
      if(p.mode==='ignition'){p.onIgnition(point);p.onDone();return;}
      if(p.mode!=='area'||!m.getSource('preview'))return;
      if(!first.current){first.current=point;return;}
      const box=bounds(first.current,point);first.current=null;data(m,'preview',empty);
      if(box[2]-box[0]<.0001||box[3]-box[1]<.0001)return;
      p.onArea(box);p.onDone();
    });
    m.on('mousemove',(event:MapMouseEvent)=>{if(current.current.mode==='area'&&first.current)data(m,'preview',rectangle(bounds(first.current,[event.lngLat.lng,event.lngLat.lat])));});
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')current.current.onDone();};
    window.addEventListener('keydown',escape);
    const observer=new ResizeObserver(()=>m.resize());observer.observe(container.current!);
    return()=>{window.removeEventListener('keydown',escape);observer.disconnect();m.remove();map.current=null;setReady(false);};
  },[]);
  useEffect(()=>{if(!ready||!map.current)return;first.current=null;data(map.current,'preview',empty);map.current.getCanvas().style.cursor=props.mode?'crosshair':'';
    if(props.mode)map.current.doubleClickZoom.disable();else map.current.doubleClickZoom.enable();},[ready,props.mode]);
  useEffect(()=>{if(ready&&map.current)data(map.current,'area',rectangle(props.bbox));},[ready,props.bbox]);
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
  return <div className="map" ref={container} aria-label="Карта участка обследования"/>;
}
