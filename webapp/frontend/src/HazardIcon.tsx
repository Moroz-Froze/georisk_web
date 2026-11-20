import {appUrl} from './deployment';
import type {Hazard} from './types';

export default function HazardIcon({hazard,size=32}:{hazard:Hazard;size?:number}){
  return <img className="hazard-icon" src={appUrl(`/icons/${hazard}.png`)} width={size} height={size} alt="" aria-hidden="true" draggable={false}/>;
}
