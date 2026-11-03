import { render } from 'preact';
import './styles/base.css';
import './lib/theme';
import { App } from './App';

render(<App />, document.getElementById('app')!);
