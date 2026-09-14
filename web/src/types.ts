export interface BattenSummary {
  batten_id: string;
  capacity_grams: number;
  total_grams: number;
  remaining_grams: number;
  load_count?: number;
}

export interface LoadItem {
  load_id: number;
  piece_id: string;
  weight_grams: number;
  created_at: string | null;
}

export interface BattenDetail extends BattenSummary {
  loads: LoadItem[];
}

export interface LoadResult {
  accepted: boolean;
  reason?: string;
  message: string;
  batten_id?: string;
  capacity_grams?: number;
  total_grams?: number;
  remaining_grams?: number;
}

export interface TransferResult {
  accepted: boolean;
  reason?: string;
  message: string;
  load_id?: number;
  piece_id?: string;
  weight_grams?: number;
  source_batten_id?: string;
  target_batten_id?: string;
  source?: BattenSummary;
  target?: BattenSummary;
}

export interface CorrectResult {
  accepted: boolean;
  reason?: string;
  message: string;
  load_id?: number;
  piece_id?: string;
  previous_weight_grams?: number;
  weight_grams?: number;
  batten_id?: string;
  capacity_grams?: number;
  total_grams?: number;
  remaining_grams?: number;
}

export interface RemoveResult {
  accepted: boolean;
  reason?: string;
  message: string;
  load_id?: number;
  piece_id?: string;
  // 本次释放的重量：首次拆下为该片重量，重复拆下为 0
  released_grams?: number;
  // 是否为重复拆下（已处于拆下状态，容量未重复释放）
  already_removed?: boolean;
  removed_at?: string | null;
  weight_grams?: number;
  batten_id?: string;
  capacity_grams?: number;
  total_grams?: number;
  remaining_grams?: number;
}

// 日检结论：只能由服务端判定（PASS 合格 / NEEDS_ATTENTION 需处理）
export type InspectionConclusion = 'PASS' | 'NEEDS_ATTENTION';

export interface InspectionRecord {
  inspection_id: number;
  batten_id: string;
  inspection_date: string;
  brake_ok: boolean;
  rope_ok: boolean;
  limit_ok: boolean;
  abnormality_note: string | null;
  conclusion: InspectionConclusion;
  conclusion_label: string;
  created_at: string | null;
}

// 日检提交体：只含检查事实，没有结论字段
export interface InspectionSubmission {
  inspection_date: string;
  brake_ok: boolean;
  rope_ok: boolean;
  limit_ok: boolean;
  abnormality_note: string | null;
}

export interface InspectionResult {
  accepted: boolean;
  reason?: string;
  message: string;
  inspection?: InspectionRecord;
}

export interface InspectionList {
  batten_id: string;
  inspections: InspectionRecord[];
}
