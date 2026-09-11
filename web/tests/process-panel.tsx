import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ProcessPanel } from "../src/processes/ProcessPanel";
import type { BackgroundProcesses } from "../src/types";
import "../src/styles.css";

function Fixture() {
  const [data, setData] = useState<BackgroundProcesses | null>(null);
  const [error, setError] = useState<string>();
  async function refresh() {
    const response = await fetch("/api/processes");
    if (response.ok) {
      setData(await response.json());
      setError(undefined);
    } else setError("Проверочная ошибка обновления");
  }
  useEffect(() => { void refresh(); }, []);
  return <main style={{ maxWidth: 1000, margin: "24px auto" }}>
    <ProcessPanel data={data} loadError={error} onSaved={setData} onRefresh={() => void refresh()} />
  </main>;
}

createRoot(document.getElementById("root")!).render(<Fixture />);
