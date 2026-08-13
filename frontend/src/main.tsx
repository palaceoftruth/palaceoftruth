import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { browserSessionBootstrap, browserSessionMigrationPending } from "./browserSessionBootstrap";

const render = () => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
};

if (browserSessionMigrationPending) {
  void browserSessionBootstrap.finally(render);
} else {
  render();
}
