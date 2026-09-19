const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('galaxypron', {
    versions: {
        electron: process.versions.electron,
        chrome: process.versions.chrome,
        node: process.versions.node,
    },
    isDesktop: true,
});
