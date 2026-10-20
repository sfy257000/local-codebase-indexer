interface ScrapPayload {
  lotNo: string;
  reasonCode: string;
  qty: number;
}

export async function submitScrapForm(payload: ScrapPayload): Promise<void> {
  const res = await fetch('/api/scrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error('scrap submit failed: ' + res.status);
  }
}
