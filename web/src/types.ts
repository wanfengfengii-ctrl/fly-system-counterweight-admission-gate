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
