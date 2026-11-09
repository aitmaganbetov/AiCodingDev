/**
 * Ревью кода (UI redesign B5, ADR-0015, API_DESIGN §5.6): треды inline-комментариев к строкам diff, ответы,
 * правка своего комментария (с историей), resolve/unresolve и отправка ревью (APPROVE / REQUEST_CHANGES / COMMENT).
 *
 * Тела комментариев — недоверенный текст людей (секреты редактирует сервер): UI выводит их только как текст.
 * Агентам комментарии не доставляются; воздействие на исполнение — только `submitReview`.
 */
import { request, seg, type JsonBody, type Query, type Schemas } from './client';

export type ReviewThread = Schemas['ReviewThread'];
export type ReviewThreadPage = Schemas['ReviewThreadPage'];
export type ReviewComment = Schemas['ReviewComment'];
export type ReviewAnchor = Schemas['ReviewAnchor'];
export type ReviewAnchorInput = Schemas['ReviewAnchorInput'];
export type ReviewThreadStatus = Schemas['ReviewThreadStatus'];
export type ReviewCommentRevisionList = Schemas['ReviewCommentRevisionList'];
export type ReviewSubmission = Schemas['ReviewSubmission'];
export type ReviewSubmissionPage = Schemas['ReviewSubmissionPage'];
export type ReviewVerdict = Schemas['ReviewVerdict'];
export type ReviewThreadCreateRequest = JsonBody<'createReviewThread'>;
export type ReviewSubmitRequest = JsonBody<'submitReview'>;
export type ListReviewThreadsQuery = Query<'listReviewThreads'>;
export type ReviewSide = ReviewAnchor['side'];

/** Лимиты контракта (ReviewCommentCreateRequest.body, ReviewSubmitRequest.thread_ids, ReviewAnchorInput). */
export const REVIEW_BODY_MAX = 8000;
export const REVIEW_THREADS_MAX = 50;
export const REVIEW_ANCHOR_MAX_LINES = 1000;

export const reviewKeys = {
  /** Префикс всех тредов run (инвалидация по SSE review.comment). */
  threadsOfRun: (runId: string) => ['review-threads', runId] as const,
  threads: (runId: string, query: ListReviewThreadsQuery & { repository_id: string }) => ['review-threads', runId, query] as const,
  revisions: (commentId: string) => ['review-comment', commentId, 'revisions'] as const,
  submissions: (runId: string) => ['reviews', runId] as const,
};

export const reviewsApi = {
  listThreads(
    projectId: string,
    repositoryId: string,
    query: ListReviewThreadsQuery = {},
    signal?: AbortSignal,
  ): Promise<ReviewThreadPage> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/repositories/${seg(repositoryId)}/review-threads`, query, signal });
  },

  /** Тред с первым комментарием (201). Событие SSE review.comment (без тела). */
  createThread(projectId: string, repositoryId: string, body: ReviewThreadCreateRequest, idempotencyKey: string): Promise<ReviewThread> {
    return request({
      method: 'POST',
      path: `/projects/${seg(projectId)}/repositories/${seg(repositoryId)}/review-threads`,
      body,
      idempotencyKey,
    });
  },

  getThread(threadId: string, query: Query<'getReviewThread'> = {}, signal?: AbortSignal): Promise<ReviewThread> {
    return request({ method: 'GET', path: `/review-threads/${seg(threadId)}`, query, signal });
  },

  reply(threadId: string, body: JsonBody<'replyReviewThread'>, idempotencyKey: string): Promise<ReviewComment> {
    return request({ method: 'POST', path: `/review-threads/${seg(threadId)}/comments`, body, idempotencyKey });
  },

  /** OPEN → RESOLVED; If-Match — версия треда, которую видел человек (412 — тред изменился). */
  resolve(threadId: string, version: number, idempotencyKey: string): Promise<ReviewThread> {
    return request({ method: 'POST', path: `/review-threads/${seg(threadId)}/resolve`, idempotencyKey, ifMatchVersion: version });
  },

  unresolve(threadId: string, version: number, idempotencyKey: string): Promise<ReviewThread> {
    return request({ method: 'POST', path: `/review-threads/${seg(threadId)}/unresolve`, idempotencyKey, ifMatchVersion: version });
  },

  /** Только автор (иначе 403); If-Match — revision комментария; прежнее тело уходит в историю правок. */
  editComment(commentId: string, body: JsonBody<'editReviewComment'>, revision: number): Promise<ReviewComment> {
    return request({ method: 'PATCH', path: `/review-comments/${seg(commentId)}`, body, ifMatchVersion: revision });
  },

  revisions(commentId: string, signal?: AbortSignal): Promise<ReviewCommentRevisionList> {
    return request({ method: 'GET', path: `/review-comments/${seg(commentId)}/revisions`, signal });
  },

  listSubmissions(runId: string, query: Query<'listReviews'> = {}, signal?: AbortSignal): Promise<ReviewSubmissionPage> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/reviews`, query, signal });
  },

  /**
   * 202 — ревью сохранено. APPROVE — решение существующего approval (step-up/four-eyes решает сервер),
   * REQUEST_CHANGES — поручение CHANGE_REQUEST (принято, не выполнено: дальше — impact preview и подтверждение).
   */
  submit(runId: string, body: ReviewSubmitRequest, idempotencyKey: string): Promise<ReviewSubmission> {
    return request({ method: 'POST', path: `/runs/${seg(runId)}/reviews`, body, idempotencyKey });
  },
};
