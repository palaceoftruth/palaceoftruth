import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import {
  browserSessionBootstrap,
  browserSessionMigrationPending,
  startBrowserSessionMaintenance,
} from "./browserSessionBootstrap";

const render = () => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  startBrowserSessionMaintenance();
};

if (browserSessionMigrationPending) {
  void browserSessionBootstrap.finally(render);
} else {
  render();
}
