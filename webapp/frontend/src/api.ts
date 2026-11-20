import {appUrl} from './deployment';

let csrf='';
const requests=new Set<AbortController>();

export function setSessionCsrf(value:string){csrf=value;}
export function abortApiRequests(){for(const request of requests)request.abort();requests.clear();}

export async function api<T>(path:string, options:RequestInit={}):Promise<T> {
  const controller=new AbortController();requests.add(controller);
  const abort=()=>controller.abort();
  if(options.signal?.aborted)controller.abort();else options.signal?.addEventListener('abort',abort,{once:true});
  const headers=new Headers(options.headers);
  if(csrf&&!['GET','HEAD','OPTIONS'].includes((options.method||'GET').toUpperCase()))headers.set('X-GeoRisk-CSRF',csrf);
  try{
    const response=await fetch(appUrl('/api'+path),{...options,credentials:'same-origin',headers,signal:controller.signal});
    const data=await response.json().catch(()=>({detail:'Сервер вернул неизвестный ответ.'}));
    if(!response.ok){
      if(response.status===401)window.dispatchEvent(new Event('georisk-session-expired'));
      const detail=data.detail;
      throw new Error(Array.isArray(detail)?detail.map((x:{message?:string;msg?:string})=>x.message||x.msg).join('\n'):String(detail||'Не удалось выполнить запрос.'));
    }
    return data;
  }finally{requests.delete(controller);options.signal?.removeEventListener('abort',abort);}
}
