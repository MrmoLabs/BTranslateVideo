const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const catalog=JSON.parse(fs.readFileSync(path.join(root,'app/static/locales/en.json'),'utf8'));
const storage={};
const element={dataset:{i18n:'保存校对'},textContent:''};
const context=vm.createContext({
  document:{documentElement:{lang:''},querySelectorAll:selector=>selector==='[data-i18n]'?[element]:[]},
  localStorage:{getItem:key=>storage[key],setItem:(key,value)=>storage[key]=value},
  fetch:async()=>({ok:true,json:async()=>catalog}),console,assert,
});
vm.runInContext(fs.readFileSync(path.join(root,'app/static/i18n.js'),'utf8'),context);
vm.runInContext(`I18N.ready().then(()=>{
  I18N.setLocale('en');I18N.applyStatic();
  assert.equal(tr('保存校对'),'Save review');
  assert.equal(document.documentElement.lang,'en');
  assert.equal(tr('第 {0} 句 · {1}{2}',3,'用户原文：保存设置',''),'Sentence 3 · 用户原文：保存设置');
  I18N.setLocale('zh-CN');I18N.applyStatic();
  assert.equal(tr('保存校对'),'保存校对');
  console.log('UI locale, persistence, placeholders and user-data isolation: OK');
}).catch(error=>{console.error(error);throw error;});`,context);
