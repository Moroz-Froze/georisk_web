import {test} from 'node:test';
import assert from 'node:assert/strict';
import {estimateArea,loadAreas,AREA_STORAGE} from '../src/area.ts';

const options={hazard:'landslide',buffer:500,resolution:30};
test('small area fits while large area is rejected',()=>{
  assert.equal(estimateArea([86,50,86.01,50.01],options).valid,true);
  const large=estimateArea([86,50,87,51],options);
  assert.equal(large.valid,false);assert.ok(large.area_km2>2500);
});
test('feedback responds to hazard, resolution and buffer',()=>{
  const box=[86,50,86.2,50.2];
  assert.equal(estimateArea(box,options).valid,true);
  assert.equal(estimateArea(box,{...options,hazard:'wildfire'}).valid,false);
  assert.equal(estimateArea(box,{...options,hazard:'wildfire',resolution:60}).valid,true);
  assert.equal(estimateArea(box,{...options,resolution:1}).valid,false);
  assert.equal(estimateArea(box,{...options,buffer:20000}).valid,false);
});
test('unsupported coordinates and invalid grid settings cannot be calculated',()=>{
  for(const box of [[86,81,86.1,82],[86,50,93,51],[87,50,86,51]])assert.equal(estimateArea(box,options).valid,false);
  assert.equal(estimateArea([86,50,86.1,50.1],{...options,resolution:0}).valid,false);
});
test('stored areas survive reload and invalid saved entries are ignored',()=>{
  const area={id:'one',bbox:[86,50,86.1,50.1],created_at:'2026-11-13'};
  const storage={getItem(key){assert.equal(key,AREA_STORAGE);return JSON.stringify([area,{...area},null,{id:'broken',bbox:[0,1]}]);}};
  assert.deepEqual(loadAreas(storage),[area]);
  assert.deepEqual(loadAreas({getItem(){return 'corrupt';}}),[]);
  assert.deepEqual(loadAreas({getItem(){throw new Error('blocked');}}),[]);
});

test('saved areas are isolated by the current private workspace',()=>{
  const area={id:'first',bbox:[86,50,86.01,50.01],created_at:'2026-11-16'};
  const storage={getItem:key=>key==='georisk.areas.v1:one'?JSON.stringify([area]):null};
  assert.equal(loadAreas(storage,'georisk.areas.v1:one')[0].id,'first');
  assert.deepEqual(loadAreas(storage,'georisk.areas.v1:two'),[]);
  assert.deepEqual(loadAreas(storage),[]);
});
