import {useEffect,useRef,useState,type ReactNode} from 'react';
import {LoaderCircle,LogOut} from 'lucide-react';
import {abortApiRequests,api,setSessionCsrf} from './api';
import {appUrl} from './deployment';
import {AREA_STORAGE} from './area';

type Session={workspace_id:string;csrf:string;idle_seconds:number;grace_seconds:number};
type Props={children:(workspaceId:string,onEnd:()=>void)=>ReactNode};

function clearWorkspaceStorage(){
  try{
    for(let index=sessionStorage.length-1;index>=0;index--){const key=sessionStorage.key(index);if(key?.startsWith('georisk.'))sessionStorage.removeItem(key);}
    localStorage.removeItem(AREA_STORAGE);
  }catch{/* Server-side isolation and deletion do not depend on browser storage. */}
}

export default function SessionGate({children}:Props){
  const [session,setSession]=useState<Session|null>(null),[phase,setPhase]=useState<'starting'|'ready'|'ending'|'ended'|'error'|'end-error'>('starting');
  const running=useRef(true),channel=useRef<BroadcastChannel|null>(null);
  const [message,setMessage]=useState(''),[confirm,setConfirm]=useState(false);
  useEffect(()=>{
    let stopped=false,info:Session|null=null;
    const tab_id=crypto.randomUUID(),controller=new AbortController();
    const expired=()=>{running.current=false;abortApiRequests();setSessionCsrf('');clearWorkspaceStorage();setMessage('Сессия завершена. Рабочие данные удаляются с сервера.');setPhase('ended');};
    const leave=()=>{if(info&&running.current)navigator.sendBeacon(appUrl('/api/session/leave'),new Blob([JSON.stringify({tab_id,csrf:info.csrf})],{type:'application/json'}));};
    const show=(event:PageTransitionEvent)=>{if(event.persisted)window.location.reload();};
    if('BroadcastChannel' in window){channel.current=new BroadcastChannel('georisk.session');channel.current.onmessage=event=>{if(info&&event.data?.workspace_id===info.workspace_id&&event.data?.ended)expired();};}
    window.addEventListener('georisk-session-expired',expired);
    window.addEventListener('pagehide',leave);window.addEventListener('pageshow',show);
    try{localStorage.removeItem(AREA_STORAGE);}catch{/* Legacy browser data is not used. */}
    api<Session>('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tab_id}),signal:controller.signal})
      .then(value=>{if(!stopped){info=value;running.current=true;setSessionCsrf(value.csrf);setSession(value);setPhase('ready');}})
      .catch(error=>{if(!stopped){setMessage('Не удалось открыть рабочую сессию: '+error.message);setPhase('error');}});
    const heartbeat=setInterval(()=>{if(info&&running.current)void api('/session/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tab_id})}).catch(()=>{});},30000);
    return()=>{stopped=true;channel.current?.close();controller.abort();clearInterval(heartbeat);window.removeEventListener('georisk-session-expired',expired);window.removeEventListener('pagehide',leave);window.removeEventListener('pageshow',show);};
  },[]);

  async function end(){
    running.current=false;setConfirm(false);setPhase('ending');abortApiRequests();
    try{
      const result=await api<{deleted:boolean}>('/session/end',{method:'POST'});
      channel.current?.postMessage({workspace_id:session?.workspace_id,ended:true});
      setSessionCsrf('');clearWorkspaceStorage();
      setMessage(result.deleted?'Расчёты, результаты и загруженные файлы удалены.':'Сессия закрыта. Сервер завершает удаление файлов.');setPhase('ended');
    }catch(error){setMessage('Не удалось подтвердить удаление: '+(error as Error).message+'. Повторите завершение сессии.');setPhase('end-error');}
  }
  if(phase==='ready'&&session)return <>
    <div className="session-workspace" inert={confirm}>{children(session.workspace_id,()=>setConfirm(true))}</div>
    {confirm&&<div className="session-backdrop"><section className="session-card" role="dialog" aria-modal="true" aria-labelledby="session-end-title" onKeyDown={event=>{if(event.key==='Escape')setConfirm(false);}}>
      <LogOut size={27}/><h1 id="session-end-title">Завершить сессию?</h1><p>Все ваши расчёты, загруженные файлы и результаты будут удалены. Выполняющиеся задания остановятся. Если открыты другие вкладки этой сессии, они тоже завершат работу.</p>
      <div className="session-buttons"><button autoFocus onClick={()=>setConfirm(false)}>Продолжить работу</button><button className="danger-button" onClick={()=>void end()}>Удалить данные и выйти</button></div>
    </section></div>}
  </>;
  return <main className="session-screen"><section className="session-card" role="status">
    {(phase==='starting'||phase==='ending')?<><LoaderCircle className="spin" size={29}/><h1>{phase==='starting'?'Открываем рабочую сессию':'Завершаем работу и удаляем данные'}</h1></>:<>
      <h1>{phase==='ended'?'Сессия завершена':'Не удалось завершить действие'}</h1><p>{message}</p>
      {phase==='end-error'?<button className="primary" onClick={()=>void end()}>Повторить удаление</button>:<button className="primary" onClick={()=>window.location.reload()}>{phase==='ended'?'Начать новую работу':'Повторить подключение'}</button>}
    </>}
  </section></main>;
}
