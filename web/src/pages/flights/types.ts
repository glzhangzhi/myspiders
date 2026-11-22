export interface Layover { airport: string; minutes: number; change_airport: boolean }
export interface Seg { flight_no: string; from: string; to: string; dep: string; arr: string; self_transfer: boolean }
export type Baggage = 'confirmed' | 'inferred' | 'recheck';

export interface Trip {
  key: string; flights: string[]; price: number; duration: number; dep_time: string; arr_time: string;
  from: string; to: string; stops: number; layovers: Layover[]; baggage: Baggage; checked_bag: string | null;
  codeshare: boolean; segments: Seg[]; reasons?: string[];
}

export type DayStatus = 'ok' | 'empty' | 'error';
export interface DaySummary {
  date: string; leg: 'out' | 'ret'; status: DayStatus; error?: string; note?: string;
  total?: number; matched?: number; rejected?: number; best?: Trip | null; top?: Trip[]; top_rejected?: Trip[];
  has_direct?: boolean; requests: number; refilled?: boolean; cached?: boolean; fetched_at?: string | null;
  warnings?: string[]; link: string;
}

export interface Filters {
  direct_only: boolean; max_transfers: number | null; allow_long_layover: boolean; long_layover_hours: number;
  allow_recheck: boolean; recheck_min_layover_hours: number; max_duration_hours: number | null;
}

export interface StartEv {
  query_id: string; mode: 'ow' | 'rt'; origin: string; dest: string; out_days: string[]; ret_days: string[];
  total_days: number; currency: string; review_enabled: boolean;
}
export interface Stats { days: number; requests: number; refills: number; cached_days: number; errors: number; elapsed_s: number }
export interface DoneEv { query_id: string; stats: Stats; oldest_fetched_at: string | null; newest_fetched_at: string | null }

export interface DayDetail {
  date: string; origin: string; dest: string; matched: Trip[]; rejected: Trip[]; fetched_at: string | null;
  cached: boolean; requests: number; refilled: boolean; warnings: string[]; link: string;
}
export interface RoundtripRes {
  out: string; ret: string; best: Trip | null; matched: Trip[]; matched_count: number; total: number;
  cheapest_unfiltered: number | null; fetched_at: string | null; cached: boolean; requests: number;
  warnings: string[]; note: string; link: string;
}
export interface Review { summary: string; picks: { label: string; why: string }[] }
export interface Config { currency: string; max_days_oneway: number; max_days_roundtrip: number; review_enabled: boolean; busy: boolean }
