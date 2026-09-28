// Training site API (docs/training-site.md section 3). Uses the viewer's
// request helper, which sends JSON with the right Content-Type and turns
// error bodies into ApiError. The browser adds the Origin header itself.
import { request } from '../api.js';

const enc = encodeURIComponent;

export const training = {
  me: () => request('GET', '/api/me'),
  logout: () => request('POST', '/api/logout', { json: {} }),
  dashboard: () => request('GET', '/api/training/dashboard'),
  queue: (limit = 50) => request('GET', `/api/training/queue?limit=${limit}`),
  markReviewed: (documentVersion, pageIndex) => request('POST', '/api/training/pages/complete',
    { json: { document_version: documentVersion, page_index: pageIndex } }),
  models: () => request('GET', '/api/training/models'),
  datasets: () => request('GET', '/api/training/datasets'),
  buildDataset: () => request('POST', '/api/training/datasets', { json: {} }),
  train: (kind, datasetId, epochs) => request('POST', '/api/training/train',
    { json: { kind, dataset_id: datasetId, ...(epochs ? { epochs } : {}) } }),
  benchmark: (modelId, datasetId) => request('POST', '/api/training/benchmarks',
    { json: { model_id: modelId, ...(datasetId ? { dataset_id: datasetId } : {}) } }),
  jobs: (limit = 50) => request('GET', `/api/training/jobs?limit=${limit}`),
  job: (jobId) => request('GET', `/api/training/jobs/${enc(jobId)}`),
  cancel: (jobId) => request('POST', `/api/training/jobs/${enc(jobId)}/cancel`, { json: {} }),
  promote: (benchmarkJobId) => request('POST', '/api/training/promote',
    { json: { benchmark_job_id: benchmarkJobId } }),
  deactivate: (kind) => request('POST', '/api/training/deactivate', { json: { kind } }),
};
