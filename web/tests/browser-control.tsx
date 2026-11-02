import { createRoot } from "react-dom/client";
import { BrowserControlNotice } from "../src/BrowserControlNotice";
import "../src/styles.css";

createRoot(document.getElementById("root")!).render(
  <main style={{ maxWidth: 1000, margin: "24px auto", padding: 16 }}>
    <BrowserControlNotice />
  </main>,
);
