(function(){
  var dataEl=document.getElementById('gloss-data'); if(!dataEl) return;
  var data=JSON.parse(dataEl.textContent); var entries=data.entries; var groups=data.groups;
  var side=document.getElementById('gloss-side'), list=document.getElementById('gloss-list'), toggle=document.getElementById('gloss-toggle'), search=document.getElementById('gloss-search'), closeBtn=document.getElementById('gloss-close');
  var bySlug={}; entries.forEach(function(e){bySlug[e.slug]=e;});
  var animStops=[];
  var reduced=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function el(tag,cls,text){var x=document.createElement(tag); if(cls) x.className=cls; if(text!=null) x.textContent=text; return x;}
  function paras(container,text){String(text).split(/\n\s*\n/).forEach(function(p){p=p.replace(/[*`]/g,'').replace(/\s+/g,' ').trim(); if(p) container.appendChild(el('p',null,p));});}
  function render(e){
    var d=el('details','gloss-item'); d.id='g-'+e.slug; d.setAttribute('data-slug',e.slug);
    var s=el('summary'); var lvl=el('span','lvl','L'+e.level); lvl.title='Level '+e.level+': 1 any DJ, 2 curious DJ, 3 engineer';
    var t=el('span'); t.appendChild(document.createTextNode(e.term)); var sm=el('small',null,e.one_liner); t.appendChild(sm);
    s.appendChild(lvl); s.appendChild(t); d.appendChild(s);
    var b=el('div','gloss-body');
    var why=el('p','why',e.why_it_matters_here); b.appendChild(why);
    b.appendChild(el('h4',null,'In plain words')); paras(b,e.plain);
    b.appendChild(el('h4',null,'Picture')); var pre=el('pre',null,e.picture); pre.setAttribute('aria-label','ASCII picture for '+e.term); b.appendChild(pre);
    if(e.animation&&e.animation.length){
      b.appendChild(el('h4',null,'Animation')); var ap=el('pre','anim',e.animation[0]); ap.setAttribute('aria-label','ASCII animation for '+e.term); b.appendChild(ap);
      var ctl=el('div','anim-ctl'); var play=el('button',null,reduced?'Play':'Pause'); var step=el('button',null,'Step'); ctl.appendChild(play); ctl.appendChild(step); b.appendChild(ctl);
      var i=0, timer=null; function show(){ap.textContent=e.animation[i%e.animation.length];}
      function start(){if(timer) return; timer=setInterval(function(){i++;show();},550); play.textContent='Pause';}
      function stop(){if(timer){clearInterval(timer);timer=null;} play.textContent='Play';}
      animStops.push(stop);
      play.addEventListener('click',function(){timer?stop():start();}); step.addEventListener('click',function(){stop();i++;show();});
      d.addEventListener('toggle',function(){if(d.open&&!reduced) start(); else stop();});
    }
    b.appendChild(el('h4',null,'In Open DJ')); paras(b,e.in_open_dj);
    if(e.related&&e.related.length){var rel=el('div','rel'); b.appendChild(el('h4',null,'Related')); e.related.forEach(function(r){var x=bySlug[r]; if(!x) return; var btn=el('button',null,x.term); btn.type='button'; btn.addEventListener('click',function(){open(r);}); rel.appendChild(btn);}); b.appendChild(rel);}
    if(e.sources&&e.sources.length){b.appendChild(el('h4',null,'Sources')); var ul=el('ul'); e.sources.forEach(function(sr){ul.appendChild(el('li',null,sr));}); b.appendChild(ul);}
    d.appendChild(b); return d;
  }
  function build(filter){
    animStops.forEach(function(f){f();}); animStops=[];
    list.textContent=''; var q=(filter||'').trim().toLowerCase(); var shown=0;
    groups.forEach(function(g){
      var items=entries.filter(function(e){return e.group===g;}).filter(function(e){ if(!q) return true; var hay=(e.term+' '+e.aliases.join(' ')+' '+e.one_liner).toLowerCase(); return hay.indexOf(q)>=0; });
      if(!items.length) return; list.appendChild(el('div','gloss-group',g));
      items.sort(function(a,b){return a.term.localeCompare(b.term);}).forEach(function(e){list.appendChild(render(e)); shown++;});
    });
    if(!shown) list.appendChild(el('p','gloss-empty','No term matches.'));
  }
  function setOpen(on){ side.hidden=false; requestAnimationFrame(function(){side.classList.toggle('open',on);}); document.body.classList.toggle('gloss-open',on); toggle.setAttribute('aria-expanded',on?'true':'false'); if(!on) setTimeout(function(){if(!side.classList.contains('open')) side.hidden=true;},220); }
  function open(slug){ setOpen(true); var d=document.getElementById('g-'+slug); if(!d){search.value=''; build(''); d=document.getElementById('g-'+slug);} if(d){d.open=true; d.scrollIntoView({block:'start',behavior:reduced?'auto':'smooth'});} }
  toggle.addEventListener('click',function(){setOpen(!side.classList.contains('open'));});
  closeBtn.addEventListener('click',function(){setOpen(false);});
  document.addEventListener('keydown',function(ev){if(ev.key==='Escape'&&side.classList.contains('open')) setOpen(false);});
  search.addEventListener('input',function(){build(search.value);});
  build('');
  // adjustable width: drag the left edge, arrow keys on the grip, double-click or Home resets; remembered per viewer
  (function(){
    var grip=document.getElementById('gloss-grip'), root=document.documentElement, MIN=280, dragging=false;
    function maxW(){return Math.max(MIN, Math.min(innerWidth*0.9, 1100));}
    function setW(px,save){ px=Math.round(Math.max(MIN, Math.min(maxW(), px))); root.style.setProperty('--gloss-w', px+'px'); if(save){ try{localStorage.setItem('gloss-w', String(px));}catch(e){} } return px; }
    function reset(){ root.style.removeProperty('--gloss-w'); try{localStorage.removeItem('gloss-w');}catch(e){} }
    try{ var saved=parseInt(localStorage.getItem('gloss-w'),10); if(saved>0) setW(saved,false); }catch(e){}
    grip.addEventListener('pointerdown',function(ev){ dragging=true; try{grip.setPointerCapture(ev.pointerId);}catch(e){} side.classList.add('resizing'); document.body.classList.add('gloss-resizing'); grip.classList.add('active'); ev.preventDefault(); });
    grip.addEventListener('pointermove',function(ev){ if(!dragging) return; setW(innerWidth-ev.clientX,false); });
    function endDrag(){ if(!dragging) return; dragging=false; side.classList.remove('resizing'); document.body.classList.remove('gloss-resizing'); grip.classList.remove('active'); setW(side.getBoundingClientRect().width,true); }
    grip.addEventListener('pointerup',endDrag); grip.addEventListener('pointercancel',endDrag);
    grip.addEventListener('dblclick',reset);
    grip.addEventListener('keydown',function(ev){ var w=side.getBoundingClientRect().width, step=ev.shiftKey?80:24; if(ev.key==='ArrowLeft'){setW(w+step,true);ev.preventDefault();} else if(ev.key==='ArrowRight'){setW(w-step,true);ev.preventDefault();} else if(ev.key==='Home'){reset();ev.preventDefault();} });
    window.addEventListener('resize',function(){ var cur=parseInt(root.style.getPropertyValue('--gloss-w'),10); if(cur>0) setW(cur,false); });
  })();
  // dotted term links: first occurrence of each term or alias in the main text
  try{
    var main=document.querySelector('main'); var skip={PRE:1,CODE:1,SVG:1,H1:1,H2:1,H3:1,BUTTON:1,A:1,SCRIPT:1,STYLE:1,SUMMARY:1,FIGCAPTION:1};
    var cands=[]; entries.forEach(function(e){[e.term].concat(e.aliases||[]).forEach(function(a){a=String(a).trim(); if(a.length>=5) cands.push({slug:e.slug,text:a});});});
    cands.sort(function(a,b){return b.text.length-a.text.length;});
    var done={};
    document.querySelectorAll('main button.term-link[data-slug]').forEach(function(btn){ var slug=btn.getAttribute('data-slug'); if(!bySlug[slug]) return; done[slug]=true; if(!btn.title) btn.title='Open the explainer: '+bySlug[slug].one_liner; btn.addEventListener('click',function(){open(slug);}); });
    cands.forEach(function(c){
      if(done[c.slug]) return;
      var re=new RegExp('(^|[^A-Za-z0-9-])('+c.text.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')(?![A-Za-z0-9-])','i');
      var walker=document.createTreeWalker(main,NodeFilter.SHOW_TEXT,{acceptNode:function(n){var p=n.parentNode; while(p&&p!==main){ if(skip[p.nodeName]||(p.classList&&(p.classList.contains('src')||p.classList.contains('num')||p.classList.contains('gloss')))) return NodeFilter.FILTER_REJECT; p=p.parentNode; } return NodeFilter.FILTER_ACCEPT;}});
      var node; while((node=walker.nextNode())){
        var m=re.exec(node.nodeValue); if(!m) continue;
        var start=m.index+m[1].length, end=start+m[2].length;
        var before=node.nodeValue.slice(0,start), mid=node.nodeValue.slice(start,end), after=node.nodeValue.slice(end);
        var btn=document.createElement('button'); btn.type='button'; btn.className='term-link'; btn.textContent=mid; btn.title='Open the explainer: '+bySlug[c.slug].one_liner; btn.setAttribute('data-slug',c.slug);
        btn.addEventListener('click',(function(slug){return function(){open(slug);};})(c.slug));
        var parent=node.parentNode; parent.insertBefore(document.createTextNode(before),node); parent.insertBefore(btn,node); node.nodeValue=after; done[c.slug]=true; break;
      }
    });
  }catch(err){ /* term links are a convenience; the sidebar still works */ }
  if(location.hash&&location.hash.indexOf('#g-')===0) open(location.hash.slice(3));
})();
