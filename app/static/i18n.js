'use strict';
// Translate only authored UI strings. Project names, scripts and logs are data.
const I18N=(()=>{
  let current='zh-CN',english={};
  try{if(localStorage.getItem('btv-ui-language')==='en')current='en';}catch{}
  function tr(source,...values){
    const template=current==='en'?(english[source]??source):source;
    return String(template).replace(/\{(\d+)\}/g,(match,index)=>index<values.length?String(values[index]):match);
  }
  function setLocale(locale){
    current=locale==='en'?'en':'zh-CN';
    document.documentElement.lang=current;
    try{localStorage.setItem('btv-ui-language',current);}catch{}
  }
  function applyStatic(){
    document.querySelectorAll('[data-i18n]').forEach(element=>{
      element.textContent=tr(element.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-attrs]').forEach(element=>{
      const attributes=JSON.parse(element.dataset.i18nAttrs);
      Object.entries(attributes).forEach(([name,source])=>element.setAttribute(name,tr(source)));
    });
  }
  const ready=fetch('/static/locales/en.json?v=20261029-languages2').then(response=>{
    if(!response.ok)throw Error('UI language catalog could not be loaded');
    return response.json();
  }).then(value=>{english=value;setLocale(current);applyStatic();}).catch(()=>{setLocale('zh-CN');applyStatic();const chooser=document.getElementById?.('ui-language');if(chooser)chooser.value='zh-CN';});
  return {tr,setLocale,applyStatic,locale:()=>current,ready:()=>ready};
})();
const tr=(source,...values)=>I18N.tr(source,...values);
