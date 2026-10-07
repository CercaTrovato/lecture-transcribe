const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL, fileURLToPath} = require('node:url');
const AdmZip = require('adm-zip');

class ComponentManager {
  constructor(root, manifestLocation) {
    this.root = path.resolve(root);
    this.manifestLocation = manifestLocation;
    this.manifest = null;
    this.active = false;
  }
  async loadManifest() {
    const location = this.manifestLocation;
    if (!location) throw Error('发布者尚未配置组件下载清单。开发模式可指定 LT_RUNTIME_MANIFEST。');
    const url = /^https?:|^file:/.test(location) ? new URL(location) : pathToFileURL(path.resolve(location));
    this.base = url;
    this.manifest = url.protocol === 'file:' ? JSON.parse(fs.readFileSync(fileURLToPath(url),'utf8')) : await (await fetch(url)).json();
    if (this.manifest.schema !== 1 || !this.manifest.components) throw Error('组件清单格式错误');
    if(this.manifest.base_url) this.base=new URL(this.manifest.base_url);
    return this.manifest;
  }
  directory(id) {
    const component = this.manifest?.components[id];
    if (!component || !/^[a-z0-9-]+$/.test(id) || !/^[a-f0-9]{64}$/.test(component.sha256) || !Number.isSafeInteger(component.bytes) || component.bytes<=0) throw Error('未知组件或无效校验值');
    return path.join(this.root,'components',`${id}-${component.sha256.slice(0,12)}`);
  }
  installed(id) {
    try {
      const folder = this.directory(id), receipt = JSON.parse(fs.readFileSync(path.join(folder,'receipt.json'),'utf8'));
      const expected=this.manifest.components[id],executable=path.resolve(folder,expected.entry);
      return receipt.sha256===expected.sha256 && receipt.entry===expected.entry && executable.startsWith(folder+path.sep) && fs.existsSync(executable);
    } catch { return false; }
  }
  repair(id) {
    if(this.active) throw Error('组件正在安装');
    const folder=this.directory(id),parent=path.join(this.root,'components');
    if(fs.existsSync(folder)) {
      if(fs.lstatSync(folder).isSymbolicLink() || !fs.realpathSync(folder).startsWith(fs.realpathSync(parent)+path.sep)) throw Error('组件目录指向外部路径，拒绝删除');
      fs.rmSync(folder,{recursive:true,force:true});
    }
  }
  async install(id, progress=()=>{}, signal) {
    if (this.active) throw Error('另一个组件正在安装');
    const component = this.manifest.components[id], destination = this.directory(id);
    if (this.installed(id)) return destination;
    this.active = true;
    const downloads = path.join(this.root,'downloads');
    const part = path.join(downloads,`${id}-${component.sha256}.part`);
    const stage = path.join(this.root,'staging',crypto.randomUUID());
    try {
      fs.mkdirSync(downloads,{recursive:true});
      let offset = fs.existsSync(part) ? fs.statSync(part).size : 0;
      if (offset > component.bytes) { fs.unlinkSync(part); offset=0; }
      const url = new URL(component.url,this.base);
      if (offset < component.bytes) {
        let chunks;
        if (url.protocol === 'file:') chunks=fs.createReadStream(fileURLToPath(url),{start:offset});
        else {
          const response = await fetch(url,{headers:offset?{Range:`bytes=${offset}-`}:{},signal});
          if (!response.ok) throw Error(`组件下载失败 HTTP ${response.status}`);
          if (offset && response.status===206) {
            if (!response.headers.get('content-range')?.startsWith(`bytes ${offset}-`)) throw Error('续传位置不匹配');
          } else offset=0;
          chunks=response.body;
        }
        const output=fs.openSync(part,offset?'a':'w');
        try {
          for await (const chunk of chunks) {
            if (signal?.aborted) throw Error('下载已取消，可重试继续');
            fs.writeSync(output,chunk); offset+=chunk.length;
            progress({state:'downloading',bytes:offset,total:component.bytes});
            if (offset>component.bytes) throw Error('下载大小超过清单声明');
          }
        } finally { fs.closeSync(output); }
      }
      progress({state:'verifying',bytes:offset,total:component.bytes});
      const hash=crypto.createHash('sha256');
      for await (const chunk of fs.createReadStream(part)) {
        if (signal?.aborted) throw Error('下载已取消，可重试继续');
        hash.update(chunk);
      }
      if (fs.statSync(part).size!==component.bytes || hash.digest('hex')!==component.sha256) {
        fs.unlinkSync(part); throw Error('组件校验失败，文件已丢弃，请重试');
      }
      const zip=new AdmZip(part);
      for (const entry of zip.getEntries()) {
        const target=path.resolve(stage,entry.entryName);
        if (!target.startsWith(stage+path.sep) || ((entry.attr>>>16)&0o170000)===0o120000) throw Error('组件压缩包包含不安全路径');
      }
      fs.mkdirSync(stage,{recursive:true});
      zip.extractAllTo(stage,true);
      for (const entry of zip.getEntries()) {
        if (!entry.isDirectory && process.platform!=='win32') fs.chmodSync(path.join(stage,entry.entryName),(entry.attr>>>16)&0o777 || 0o644);
      }
      const executable=path.resolve(stage,component.entry);
      if (!executable.startsWith(stage+path.sep) || !fs.existsSync(executable)) throw Error('组件入口缺失');
      fs.writeFileSync(path.join(stage,'receipt.json'),JSON.stringify(component));
      fs.mkdirSync(path.dirname(destination),{recursive:true});
      if (fs.existsSync(destination)) throw Error('已有组件目录损坏，请先通过修复操作处理');
      fs.renameSync(stage,destination);
      fs.unlinkSync(part);
      progress({state:'installed',bytes:component.bytes,total:component.bytes});
      return destination;
    } finally {
      if (fs.existsSync(stage) && path.dirname(stage)===path.join(this.root,'staging')) fs.rmSync(stage,{recursive:true,force:true});
      this.active=false;
    }
  }
}
module.exports={ComponentManager};
