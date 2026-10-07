const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('desktop',{
  pickModelPath: directory=>ipcRenderer.invoke('pick-model',directory),
  runtimeStatus: ()=>ipcRenderer.invoke('runtime-status'),
  runtimeSettings: ()=>ipcRenderer.invoke('runtime-settings'),
  installRuntime: id=>ipcRenderer.invoke('install-runtime',id),
  cancelRuntime: ()=>ipcRenderer.invoke('cancel-runtime'),
  repairRuntime: id=>ipcRenderer.invoke('repair-runtime',id),
  launch: ()=>ipcRenderer.invoke('launch'),
  onProgress: callback=>ipcRenderer.on('runtime-progress',(_,value)=>callback(value)),
});
