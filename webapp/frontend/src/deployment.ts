/** Resolve internal resources beside the page, at / or an IIS subdirectory. */
export function appUrl(path:string, pageUrl:string=document.baseURI):string {
  if(!path || path.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(path) || path.includes('\\'))
    throw new Error('Expected an application-relative path');
  const base=new URL('.',pageUrl);
  const result=new URL(path.replace(/^\//,''),base);
  if(result.origin!==base.origin || !result.pathname.startsWith(base.pathname))
    throw new Error('Path must stay inside the application');
  return result.pathname+result.search+result.hash;
}
