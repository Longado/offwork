import { Prototype } from "./Prototype.jsx";
import { Storyboard } from "./Storyboard.jsx";
import "./prototype.css";

export function App() {
  const params = new URLSearchParams(window.location.search);
  return params.has("scene") ? <Storyboard /> : <Prototype />;
}
