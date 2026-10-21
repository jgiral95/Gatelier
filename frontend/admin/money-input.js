/* Campos de dinero con punto de mil mientras se escribe (35.000) y coma decimal (1.250,50).
   El campo se muestra formateado, pero .value sigue devolviendo el número limpio ("35000", "1250.5"),
   así el resto del código y la API no cambian. Se activa con el atributo data-money. */
(()=>{
  const native=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
  const group=s=>s.replace(/\B(?=(\d{3})+(?!\d))/g,'.');
  // Texto mostrado ("35.000,5") → número limpio ("35000.5")
  function clean(display){
    let s=String(display??'').replace(/[^\d,]/g,'');
    const i=s.indexOf(',');
    if(i>=0) s=s.slice(0,i)+'.'+s.slice(i+1).replace(/,/g,'').slice(0,2);
    s=s.replace(/^0+(?=\d)/,'');
    return s.startsWith('.')?'0'+s:s;
  }
  // Número limpio → texto mostrado
  function show(canon){
    if(canon===''||canon==null) return '';
    let [int,dec]=String(canon).split('.');
    int=group(int.replace(/\D/g,'')||'0');
    return dec!==undefined?`${int},${dec}`:int;
  }
  // Valor puesto por el código (75000, "75000.00", "35.000") → número limpio
  function fromCode(v){
    const s=String(v??'').trim();
    if(s==='') return '';
    if(/^-?\d+(\.\d+)?$/.test(s)){const [i,d]=s.split('.');const dd=(d||'').replace(/0+$/,'').slice(0,2);return dd?`${i}.${dd}`:i}
    return clean(s);
  }
  function enhance(el){
    if(el.dataset.moneyReady) return;
    el.dataset.moneyReady='1';
    const start=fromCode(native.get.call(el));
    el.type='text'; el.inputMode='decimal'; el.autocomplete='off';
    if(!el.placeholder) el.placeholder='0';
    Object.defineProperty(el,'value',{configurable:true,
      get(){return clean(native.get.call(el))},
      set(v){native.set.call(el,show(fromCode(v)))}});
    native.set.call(el,show(start));
    el.addEventListener('input',()=>{
      const before=native.get.call(el),caret=el.selectionStart??before.length;
      // Cuántos dígitos (y coma) había antes del cursor, para devolverlo al mismo sitio.
      const keep=before.slice(0,caret).replace(/[^\d,]/g,'').length;
      const next=show(clean(before))+(before.endsWith(',')&&!clean(before).includes('.')?',':'');
      if(next===before) return;
      native.set.call(el,next);
      let pos=0,seen=0;
      while(pos<next.length&&seen<keep){if(/[\d,]/.test(next[pos]))seen++;pos++}
      try{el.setSelectionRange(pos,pos)}catch(_){}
    });
  }
  const scan=root=>{if(root.matches&&root.matches('input[data-money]'))enhance(root);root.querySelectorAll&&root.querySelectorAll('input[data-money]').forEach(enhance)};
  scan(document);
  new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>n.nodeType===1&&scan(n)))).observe(document.documentElement,{childList:true,subtree:true});
})();
