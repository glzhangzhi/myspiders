import { render } from 'preact';
import './styles/base.css';
import './lib/theme';
import { installTips } from './lib/tip';
import { App } from './App';

installTips();
render(<App />, document.getElementById('app')!);
