export async function api<T>(path:string, options:RequestInit={}):Promise<T> {
  const response = await fetch('/api'+path, options);
  const data = await response.json().catch(()=>({detail:'Сервер вернул неизвестный ответ.'}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(Array.isArray(detail) ? detail.map((x:{message?:string;msg?:string})=>x.message||x.msg).join('\n') : String(detail||'Не удалось выполнить запрос.'));
  }
  return data;
}
