import {test,expect} from '@playwright/test';

test('MapLibre, burger drawer and bottom tools',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/');
  await expect(page.locator('.maplibregl-canvas')).toBeVisible();
  await expect(page.getByLabel('Инструменты карты',{exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Модель',exact:true})).not.toBeVisible();
  await page.getByRole('button',{name:'Открыть панель инструментов',exact:true}).click();
  await page.getByRole('button',{name:'Оползни',exact:true}).click();
  await expect(page.getByText('Обводнение: высота воды / глубина слоя')).toBeVisible();
  await page.getByRole('button',{name:'Лесные пожары',exact:true}).click();
  await expect(page.getByText('Sentinel-2 · NDVI / NDMI',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Закрыть панель инструментов',exact:true}).click();
  const tools=page.getByLabel('Инструменты карты',{exact:true});
  await tools.getByRole('button',{name:'Выделить участок',exact:true}).click();
  const canvas=page.locator('.maplibregl-canvas');
  await canvas.click({position:{x:700,y:300}});await canvas.click({position:{x:900,y:500}});
  await expect(page.getByRole('button',{name:'Показать участок',exact:true})).toBeEnabled();
  await tools.getByRole('button',{name:'Указать очаг пожара',exact:true}).click();
  await canvas.click({position:{x:800,y:400}});
  await page.getByRole('button',{name:'Открыть панель инструментов',exact:true}).click();
  await expect(page.getByRole('button',{name:'Указать очаг 1',exact:true})).toBeVisible();
  await page.screenshot({path:'../ui-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'../ui-mobile.png',fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(errors).toEqual([]);
});

test('saved result loads in the same drawer',async({page,request})=>{
  const response=await request.get('/api/jobs');const jobs=await response.json();
  test.skip(!jobs.some((j:{state:string})=>j.state==='completed'),'No completed calculation yet');
  await page.goto('/');await page.getByRole('button',{name:'Открыть панель инструментов',exact:true}).click();
  await page.getByRole('button',{name:'История',exact:true}).click();
  await page.locator('.history-card').filter({hasText:'Готово'}).first().click();
  const layer=page.locator('.layer-select select');await expect(layer).toBeVisible();
  await layer.selectOption('');await layer.selectOption({index:1});
  await expect(page.getByRole('link',{name:'Скачать ZIP',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Слои и результаты',exact:true})).toBeEnabled();
});
