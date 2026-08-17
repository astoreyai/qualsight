import React from "react";
import ReactDOM from "react-dom/client";
import { registerSW } from "virtual:pwa-register";
import { App } from "./App";
import "./styles.css";

registerSW({ immediate: true });

const container = document.getElementById("root");
if (!container) {
  throw new Error("QualSight console root element is missing");
}
ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
