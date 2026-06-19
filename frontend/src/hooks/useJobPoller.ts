import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { JobStatus } from "../api/types";

const TERMINAL = new Set(["completed", "failed", "duplicate"]);

export function useJobPoller(jobId: string | null): JobStatus | null {
  const [job, setJob] = useState<JobStatus | null>(null);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      return;
    }

    let active = true;

    const poll = async () => {
      try {
        const status = await api.getJob(jobId);
        if (!active) return;
        setJob(status);
        if (TERMINAL.has(status.status)) clearInterval(id);
      } catch {
        // ignore transient errors
      }
    };

    poll();
    const id = setInterval(poll, 2000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [jobId]);

  return job;
}
