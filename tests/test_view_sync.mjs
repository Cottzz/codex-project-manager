import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const app=fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const defs=app.match(/^const viewDefs = .*$/m)[0];
const restore=app.match(/^function restoredView.*$/m)[0];
const visible=app.match(/^function refreshVisibleState.*$/m)[0];
test('legacy, absent and malformed saved navigation restore a real overview; current views survive',()=>{
 const c=vm.createContext({});vm.runInContext(defs+'\n'+restore,c);
 for(const value of [null,'','objects','undefined','<script>'])assert.equal(c.restoredView(value),'overview');
 for(const value of ['overview','plan','frontier','graph','threads','timeline'])assert.equal(c.restoredView(value),value);
 assert.match(app,/view = restoredView\(localStorage.getItem\('pgw-view'\)\)/);
});
test('returning to a visible workspace reads current state without mutating it',async()=>{
 let reads=0;const editor={open:false};const c=vm.createContext({state:{sequence:1051},document:{hidden:false},$:()=>editor,refresh:async force=>{assert.equal(force,true);reads++;}});
 vm.runInContext(visible,c);await c.refreshVisibleState();assert.equal(reads,1);
 c.document.hidden=true;await c.refreshVisibleState();assert.equal(reads,1);
 c.document.hidden=false;editor.open=true;await c.refreshVisibleState();assert.equal(reads,1);
 editor.open=false;c.state=null;await c.refreshVisibleState();assert.equal(reads,1);
 assert.match(app,/document.addEventListener\('visibilitychange',refreshVisibleState\)/);
 assert.match(app,/window.addEventListener\('focus',refreshVisibleState\)/);
});
