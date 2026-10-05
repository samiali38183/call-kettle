/* Runs real browser code with an offline DOM/media seam; no fabricated media decoding. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class El {
  constructor(name='') { this.name=name; this.children=[]; this.attrs={}; this.handlers={}; this._text=''; this._html=''; this.className=''; this.disabled=false; this.scrollTop=0; this.scrollHeight=100; this.classList={add:()=>{},remove:()=>{},toggle:()=>{}}; }
  get textContent(){return this._text;} set textContent(v){this._text=String(v);}
  get innerHTML(){return this._html || this._text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  set innerHTML(v){this._html=v;this.children=[];}
  setAttribute(k,v){this.attrs[k]=v;} getAttribute(k){return this.attrs[k];}
  addEventListener(k,f){(this.handlers[k] ||= []).push(f);}
  emit(k,event={}){for(const f of this.handlers[k]||[]) f({target:this,...event});}
  appendChild(e){this.children.push(e);} remove(){} focus(){} querySelector(){return null;}
}
const selectors={}; for(const s of ['.cu-body','.cu-status','.cu-result','[data-cu-title]','[data-cu-sub]','[data-cu-play]','[data-cu-replay]','[data-cu-transcript]','[data-cu-audio-note]','[data-cu-pause]']) selectors[s]=new El(s);
const audio=new El('audio'); Object.assign(audio,{paused:true,currentTime:0,duration:12,src:'',playCalls:0,load(){this.currentTime=0;this.paused=true;},pause(){this.paused=true;this.emit('pause');},play(){this.playCalls++;this.paused=false;this.emit('play');return Promise.resolve();}}); selectors['[data-cu-audio]']=audio;
const tabs=[new El('HVAC'),new El('Plumbing')]; const root=new El('root');root.querySelector=s=>selectors[s]||null;root.querySelectorAll=s=>s==='.cu-tab'?tabs:[];
const calls=[{slug:'hvac',label:'HVAC',business:'Sample HVAC',turns:[{who:'ai',text:'HVAC greeting'},{who:'caller',text:'HVAC customer'},{who:'ai',text:'HVAC confirmed'}],booking:{service:'Repair',name:'Fictional Caller',when:'Recorded sample'},audio:{src:'/assets/call-example-hvac.mp3',duration:12,cues:[0,4,8]}},{slug:'plumbing',label:'Plumbing',business:'Sample Plumbing',turns:[{who:'ai',text:'Plumbing greeting'},{who:'caller',text:'Plumbing customer'}],audio:{src:'/assets/call-example-plumbing.mp3',duration:12,cues:[0,6]}}];
const data=new El();data.textContent=JSON.stringify(calls);
const document={getElementById:k=>k==='callui'?root:k==='calls-data'?data:null,querySelectorAll:()=>[],createElement:()=>new El()};
const context={document,window:{matchMedia:()=>({matches:false})},navigator:{},console,setTimeout,clearTimeout,Promise,IntersectionObserver:class{observe(){}disconnect(){}}};vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
(async()=>{
 assert.equal(audio.src,calls[0].audio.src,'Selected test call must load its matching audio');
 assert.equal(audio.playCalls,0,'No audio or transcript autoplay before the visitor starts');
 selectors['[data-cu-play]'].emit('click');await Promise.resolve();assert.equal(audio.playCalls,1);assert.equal(selectors['.cu-status'].textContent,'Playing example');
 audio.currentTime=8.5;audio.emit('timeupdate');assert.equal(selectors['.cu-body'].children.length,3,'Transcript follows audio time');
 const m=selectors['.cu-body'].children[1];m.closest=()=>m;selectors['.cu-body'].emit('click',{target:m});await Promise.resolve();assert(Math.abs(audio.currentTime-4.01)<0.02,'Clicking a message jumps the audio to that message');assert.equal(selectors['.cu-body'].children.length,2);audio.currentTime=8.5;audio.emit('timeupdate');
 selectors['[data-cu-play]'].emit('click');assert(audio.paused);assert.equal(selectors['[data-cu-play]'].textContent,'Resume');
 selectors['[data-cu-play]'].emit('click');await Promise.resolve();assert.equal(audio.currentTime,8.5,'Resume must not restart');
 tabs[1].emit('click');assert(audio.paused);assert.equal(audio.src,calls[1].audio.src);assert.equal(audio.currentTime,0);assert.equal(selectors['[data-cu-title]'].textContent,'Sample Plumbing');
 selectors['[data-cu-replay]'].emit('click');await Promise.resolve();assert.equal(audio.currentTime,0);assert(!audio.paused);
 audio.currentTime=12;audio.paused=true;audio.emit('ended');assert.equal(selectors['.cu-status'].textContent,'Example complete');assert.equal(selectors['[data-cu-play]'].textContent,'Replay this call','Ended replay must not advertise Resume');
 audio.emit('error');assert(selectors['[data-cu-play]'].disabled);assert(/unavailable/i.test(selectors['[data-cu-audio-note]'].textContent));
 const before=audio.playCalls;selectors['[data-cu-transcript]'].emit('click');assert.equal(audio.playCalls,before);assert.equal(selectors['.cu-body'].children.length,2);assert.equal(selectors['.cu-status'].textContent,'Full transcript');
 console.log('PASS selected audio, explicit start, timestamp sync, pause/resume, switch, replay, ended and unavailable fallback');
})().catch(e=>{console.error(e);process.exitCode=1;});
