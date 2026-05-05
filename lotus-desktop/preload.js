// Minimal preload: exposes a safe bridge for the setup window to save
// settings back to the main process. contextIsolation=true keeps renderer
// sandboxed; renderer only sees the whitelisted `lotus` API.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lotus', {
  saveSettings: (settings) => ipcRenderer.invoke('lotus:save-settings', settings),
  clearSettings: () => ipcRenderer.invoke('lotus:clear-settings'),
  pickFile: (opts) => ipcRenderer.invoke('lotus:pick-file', opts || {}),
});
