const {app,BrowserWindow,ipcMain,dialog}=require('electron');
const fs=require('node:fs'),path=require('node:path'),net=require('node:net');
const {spawn}=require('node:child_process');
const {ComponentManager}=require('./component-manager');
if(process.env.LT_USER_DATA){fs.mkdirSync(process.env.LT_USER_DATA,{recursive:true});app.setPath('userData',path.resolve(process.env.LT_USER_DATA));}
let window,python,manager,manifestError='',controller,backendURL,closing=false,switchingRuntime=false;
const platformKey=`${process.platform}-${process.arch}`;
const cpuId=`cpu-${platformKey}`,gpuId=`cuda-${platformKey}`;
const data=process.env.LT_DATA_DIR || path.join(app.getPath('userData'),'data');
const components=process.env.LT_COMPONENT_DIR || path.join(app.getPath('userData'),'runtime');
const source=app.isPackaged?path.join(process.resourcesPath,'python-app'):path.resolve(__dirname,'..');

async function launch() {
  if (python) {await window.loadURL(backendURL);return;}
  let executable=process.env.LT_DEV_PYTHON;
  let cpuFolder;
  if (!executable) {
    if (!manager.installed(cpuId)) throw Error('请先安装必要运行组件，模型仍由你自行选择。');
    cpuFolder=manager.directory(cpuId);
    executable=path.join(cpuFolder,manager.manifest.components[cpuId].entry);
  }
  const port=await new Promise(resolve=>{const socket=net.createServer();socket.listen(0,'127.0.0.1',()=>{const value=socket.address().port;socket.close(()=>resolve(value));});});
  const mtPort=await new Promise(resolve=>{const socket=net.createServer();socket.listen(0,'127.0.0.1',()=>{const value=socket.address().port;socket.close(()=>resolve(value));});});
  const env={...process.env,LT_DATA_DIR:data,LT_PORT:String(port),LT_MT_PORT:String(mtPort),PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8',PYTHONUNBUFFERED:'1'};
  if (cpuFolder) {
    env.LT_DEVICE=process.platform==='darwin' && process.arch==='arm64'?'auto':'cpu';
    env.PYTHONHOME=path.join(cpuFolder,'python');
    env.LT_LLAMA_SERVER=path.join(cpuFolder,'llama',process.platform==='win32'?'llama-server.exe':'llama-server');
    if (manager.manifest.components[gpuId] && manager.installed(gpuId)) {
      const gpu=manager.directory(gpuId);
      env.PYTHONPATH=path.join(gpu,'site-packages');
      env.LT_EXTRA_SITE=path.join(gpu,'site-packages');
      env.LT_LLAMA_SERVER=path.join(gpu,'llama',process.platform==='win32'?'llama-server.exe':'llama-server');
      env.LT_DEVICE='cuda';
      if(process.platform==='linux') {
        const nvidia=path.join(gpu,'site-packages','nvidia');
        const dirs=fs.existsSync(nvidia)?fs.readdirSync(nvidia).map(name=>path.join(nvidia,name,'lib')).filter(folder=>fs.existsSync(folder)):[];
        env.LD_LIBRARY_PATH=dirs.concat(process.env.LD_LIBRARY_PATH||[]).join(path.delimiter);
      }
    }
  }
  fs.mkdirSync(data,{recursive:true});
  const log=fs.openSync(path.join(data,'service.log'),'a');
  python=spawn(executable,[path.join(source,'app.py'),'--no-browser'],{cwd:source,env,windowsHide:true,stdio:['ignore',log,log]});
  fs.closeSync(log);
  let spawnError;
  python.on('error',error=>{spawnError=error;});
  python.on('exit',()=>{python=null;if(!closing && !switchingRuntime && window && !window.isDestroyed()) window.loadFile(path.join(__dirname,'setup.html'));});
  backendURL=`http://127.0.0.1:${port}`;
  for(let n=0;n<100;n++) {
    if(spawnError) throw spawnError;
    try {const response=await fetch(`${backendURL}/api/status`);if(response.ok){await window.loadURL(backendURL);return;}}catch{}
    await new Promise(resolve=>setTimeout(resolve,200));
  }
  if(python) python.kill();
  throw Error('本地服务未就绪，请查看数据目录内的 service.log。');
}

app.whenReady().then(async()=>{
  manager=new ComponentManager(components,process.env.LT_RUNTIME_MANIFEST || path.join(process.resourcesPath,'components.json'));
  try{await manager.loadManifest();}catch(error){manifestError=error.message;}
  window=new BrowserWindow({width:1320,height:850,show:!app.commandLine.hasSwitch('headless'),webPreferences:{preload:path.join(__dirname,'preload.js'),contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:!app.commandLine.hasSwitch('headless')}});
  window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  window.webContents.on('will-navigate',(event,url)=>{if(!url.startsWith(backendURL||'file://')) event.preventDefault();});
  ipcMain.handle('pick-model',async(_,directory)=>{const result=await dialog.showOpenDialog(window,{properties:[directory?'openDirectory':'openFile']});return result.canceled?null:result.filePaths[0];});
  ipcMain.handle('runtime-status',()=>({error:manifestError,platform:platformKey,components:manager.manifest?Object.entries(manager.manifest.components).filter(([id])=>id.endsWith(platformKey)).map(([id,value])=>({id,bytes:value.bytes,installed:manager.installed(id)})):[]}));
  ipcMain.handle('cancel-runtime',()=>{controller?.abort();});
  ipcMain.handle('runtime-settings',()=>window.loadFile(path.join(__dirname,'setup.html')));
  ipcMain.handle('repair-runtime',async(_,id)=>{
    if(python) throw Error('请先关闭当前服务');
    const result=await dialog.showMessageBox(window,{type:'question',message:'重新安装此运行组件？模型和录音不会删除。',buttons:['取消','重新安装'],defaultId:0,cancelId:0});
    if(result.response===1) manager.repair(id);
  });
  ipcMain.handle('install-runtime',async(_,id)=>{
    if(!id.endsWith(platformKey)) throw Error('组件与本机平台不匹配');
    controller=new AbortController();
    try{
      if(python){
        const child=python;
        const exited=new Promise(resolve=>child.once('exit',resolve));
        switchingRuntime=true;
        const response=await fetch(`${backendURL}/api/shutdown`,{method:'POST'});
        if(!response.ok){const result=await response.json();return {ok:false,error:result.detail};}
        await exited;
      }
      await manager.install(id,value=>window.webContents.send('runtime-progress',value),controller.signal);return {ok:true};
    }
    catch(error){return {ok:false,error:error.message};}
    finally{switchingRuntime=false;}
  });
  ipcMain.handle('launch',async()=>{try{await launch();return {ok:true};}catch(error){return {ok:false,error:error.message};}});
  window.on('close',async event=>{
    if(closing || !python) return;
    event.preventDefault();
    try {
      const response=await fetch(`${backendURL}/api/shutdown`,{method:'POST'});
      if(!response.ok){const data=await response.json();await dialog.showMessageBox(window,{message:data.detail||'当前任务尚未结束',type:'info'});return;}
      closing=true;window.close();
    }catch(error){await dialog.showMessageBox(window,{message:`后台服务无法安全退出：${error.message}`,type:'error'});}
  });
  if(process.env.LT_DEV_PYTHON || manager.installed(cpuId)) {
    try{await launch();}catch(error){manifestError=error.message;await window.loadFile(path.join(__dirname,'setup.html'));}
  }else await window.loadFile(path.join(__dirname,'setup.html'));
});
app.on('window-all-closed',()=>app.quit());
