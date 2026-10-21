/* Comprobantes de ventas y gastos: reduce fotos en el navegador (Nginx acepta 1 MB por petición) y las sube. */
window.Receipts=(()=>{
  const LIMIT=900000;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  async function shrink(file){
    const url=URL.createObjectURL(file);
    try{
      const im=await new Promise((ok,ko)=>{const i=new Image();i.onload=()=>ok(i);i.onerror=()=>ko(Error('No se pudo leer la imagen. Usa una foto JPG, PNG o WebP, o un PDF.'));i.src=url});
      // Los comprobantes tienen letra pequeña: se intenta primero a buen tamaño y se baja solo si hace falta.
      for(const [max,q] of [[2000,.85],[1700,.8],[1400,.75],[1200,.7],[1000,.65]]){
        const sc=Math.min(1,max/Math.max(im.naturalWidth,im.naturalHeight));
        const cv=document.createElement('canvas');cv.width=Math.round(im.naturalWidth*sc);cv.height=Math.round(im.naturalHeight*sc);
        const cx=cv.getContext('2d');cx.fillStyle='#fff';cx.fillRect(0,0,cv.width,cv.height);cx.drawImage(im,0,0,cv.width,cv.height);
        const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',q));
        if(blob&&blob.size<=LIMIT)return blob;
      }
      throw Error('La foto sigue pesando demasiado incluso reducida. Prueba con una captura de pantalla.');
    }finally{URL.revokeObjectURL(url)}
  }
  async function prepare(file){
    if(file.type==='application/pdf'||/\.pdf$/i.test(file.name)){
      if(file.size>LIMIT)throw Error('El PDF pesa más de 900 KB. Sube mejor una captura o una foto del comprobante.');
      return {blob:file,name:'comprobante.pdf'};
    }
    return {blob:await shrink(file),name:'comprobante.jpg'};
  }
  async function upload(path,file){
    const {blob,name}=await prepare(file);
    const fd=new FormData();fd.append('file',blob,name);
    const r=await fetch('/api'+path,{method:'POST',body:fd,credentials:'same-origin'});
    let d={};try{d=await r.json()}catch(_){}
    if(r.status===401){location.href='/admin/';throw Error('Sesión vencida')}
    if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'No se pudo guardar el comprobante');
    return d;
  }
  async function remove(path){
    const r=await fetch('/api'+path,{method:'DELETE',credentials:'same-origin'});let d={};try{d=await r.json()}catch(_){}
    if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'No se pudo quitar el comprobante');return d;
  }
  /* Miniatura que abre el comprobante completo en otra pestaña. */
  function thumb(rec,label='Ver comprobante'){
    if(!rec)return '';
    const inner=rec.is_pdf?'<span class="rcpt-pdf" aria-hidden="true">PDF</span>':`<img src="${esc(rec.url)}" alt="" loading="lazy">`;
    return `<a class="rcpt" href="${esc(rec.url)}" target="_blank" rel="noopener">${inner}<span>${esc(label)}</span></a>`;
  }
  /* Vista grande para el detalle de una venta. */
  function preview(rec){
    if(!rec)return '';
    const when=new Date(rec.uploaded_at).toLocaleDateString('es-CO',{day:'numeric',month:'short',year:'numeric'});
    const body=rec.is_pdf?`<a class="rcpt-big pdf" href="${esc(rec.url)}" target="_blank" rel="noopener"><span class="rcpt-pdf">PDF</span>Abrir el comprobante</a>`
      :`<a class="rcpt-big" href="${esc(rec.url)}" target="_blank" rel="noopener" title="Abrir en tamaño completo"><img src="${esc(rec.url)}" alt="Comprobante"></a>`;
    return `${body}<div class="muted">Subido el ${when}${rec.uploaded_by?' por '+esc(rec.uploaded_by):''}</div>`;
  }
  return {prepare,upload,remove,thumb,preview,ACCEPT:'image/*,application/pdf'};
})();
