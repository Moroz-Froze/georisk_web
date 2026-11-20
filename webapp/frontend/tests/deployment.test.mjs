import test from 'node:test';
import assert from 'node:assert/strict';
import {appUrl} from '../src/deployment.ts';

test('IIS subfolder keeps API, beacon and icons inside georisk',()=>{
  const base='https://innovations.sibpsa.ru/georisk/';
  assert.equal(appUrl('/api/session',base),'/georisk/api/session');
  assert.equal(appUrl('/api/session/leave',base),'/georisk/api/session/leave');
  assert.equal(appUrl('/icons/avalanche.png',base),'/georisk/icons/avalanche.png');
  assert.equal(appUrl('/api/jobs?x=1',base),'/georisk/api/jobs?x=1');
});
test('one build also works at the root and index.html',()=>{
  assert.equal(appUrl('/api/session','http://127.0.0.1:8000/'),'/api/session');
  assert.equal(appUrl('/api/session','https://example.org/georisk/index.html'),'/georisk/api/session');
});
test('local application URL cannot turn into a cross-origin request',()=>{
  for(const path of ['//evil.invalid/api','https://evil.invalid/api'])
    assert.throws(()=>appUrl(path,'https://example.org/georisk/'));
});
