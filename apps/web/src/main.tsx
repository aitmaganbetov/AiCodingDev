import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { App } from './App';
import { preloadShellNamespaces } from './i18n';
import './index.css';
import { applyTheme } from './lib/theme';
import { createQueryClient } from './queryClient';

applyTheme();
preloadShellNamespaces();

const root = document.getElementById('root');
if (!root) throw new Error('#root not found');

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <App queryClient={createQueryClient()} />
    </BrowserRouter>
  </StrictMode>,
);
