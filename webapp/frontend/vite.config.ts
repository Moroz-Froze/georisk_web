import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base:'./',
  plugins:[react()],
  server:{host:'127.0.0.1',proxy:{'/api':'http://127.0.0.1:8000'}},
  build:{rolldownOptions:{output:{manualChunks(id){
    if(id.includes('/node_modules/maplibre-gl/')) return 'maplibre';
  }}}},
});
