const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const AdmZip=require('adm-zip');
const {ComponentManager}=require('./component-manager');
test('explicit verified component install reuses existing bytes and rejects unknown component',async()=>{
  const root=fs.mkdtempSync(path.join(process.env.LT_TEST_TEMP||os.tmpdir(),'runtime-test-'));
  try {
    const zip=new AdmZip();zip.addFile('python/python.exe',Buffer.from('runtime fixture'));
    const archive=zip.toBuffer();fs.writeFileSync(path.join(root,'component.zip'),archive);
    const manifest={schema:1,components:{'cpu-win32-x64':{url:'component.zip',bytes:archive.length,sha256:crypto.createHash('sha256').update(archive).digest('hex'),entry:'python/python.exe'}}};
    fs.writeFileSync(path.join(root,'manifest.json'),JSON.stringify(manifest));
    const manager=new ComponentManager(path.join(root,'installed'),path.join(root,'manifest.json'));
    await manager.loadManifest();assert.equal(manager.installed('cpu-win32-x64'),false);
    const target=await manager.install('cpu-win32-x64');assert.equal(fs.readFileSync(path.join(target,'python/python.exe'),'utf8'),'runtime fixture');
    assert.equal(manager.installed('cpu-win32-x64'),true);
    assert.equal(await manager.install('cpu-win32-x64'),target);
    await assert.rejects(()=>manager.install('../outside'));
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
test('bad component hash cannot install any executable',async()=>{
  const root=fs.mkdtempSync(path.join(process.env.LT_TEST_TEMP||os.tmpdir(),'runtime-test-'));
  try {
    fs.writeFileSync(path.join(root,'bad.zip'),'invalid');
    const manifest={schema:1,components:{'cpu-win32-x64':{url:'bad.zip',bytes:7,sha256:'0'.repeat(64),entry:'python/python.exe'}}};
    fs.writeFileSync(path.join(root,'manifest.json'),JSON.stringify(manifest));
    const manager=new ComponentManager(path.join(root,'installed'),path.join(root,'manifest.json'));await manager.loadManifest();
    await assert.rejects(()=>manager.install('cpu-win32-x64'),/校验失败/);assert.equal(manager.installed('cpu-win32-x64'),false);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
