import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles/tokens.css';
import './styles/dashboard.css';

const rootEl = document.getElementById('root');
if (!rootEl) throw new Error('Patter dashboard: #root element missing');

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
