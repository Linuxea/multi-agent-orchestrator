/** RunStream 内部依赖的轻量类型（避免循环依赖 api/types）。 */

import type {
  MessageInfo,
  PlanVersion,
  RunDetail,
  RunListItem,
} from "../api/types";

export type { MessageInfo, PlanVersion, RunDetail, RunListItem };

export interface StreamEventLike {
  id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}
