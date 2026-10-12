import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/l10n/app_localizations.dart';
import '../../../core/l10n/app_locale_provider.dart';
import '../../../core/db/account_db_key.dart';
import '../../../core/server/api/asr_api.dart';
import '../../../core/server/server_providers.dart';
import '../data/recordings_repository.dart';
import '../domain/recording.dart';
import 'recording_transcribe_pipeline.dart';
import 'recordings_controller.dart';
import 'transcribe_common.dart';

/// Global transcription task state — survives navigation away from detail page.
class TranscriptionTaskState {
  final Map<String, String> progressByRecordingId;
  final Set<String> activeRecordingIds;

  const TranscriptionTaskState({
    this.progressByRecordingId = const {},
    this.activeRecordingIds = const {},
  });

  bool isActive(String recordingId) =>
      activeRecordingIds.contains(recordingId.trim());

  String? progressFor(String recordingId) =>
      progressByRecordingId[recordingId.trim()];
}

final transcriptionTaskControllerProvider =
    NotifierProvider<TranscriptionTaskController, TranscriptionTaskState>(
  TranscriptionTaskController.new,
);

/// Reconcile stuck `job_state` rows on startup / list resume (no UI).
final reconcileStuckTranscriptionJobsProvider = FutureProvider<void>((ref) async {
  await ref
      .read(transcriptionTaskControllerProvider.notifier)
      .reconcileStuckJobs();
});

class TranscriptionTaskController extends Notifier<TranscriptionTaskState> {
  final Map<String, bool> _cancelRequested = {};
  final Map<String, int> _activeTaskGeneration = {};
  final Map<String, Set<String>> _interruptedIdsByAccountKey = {};
  int _accountGeneration = 0;
  bool _reconcileInFlight = false;

  @override
  TranscriptionTaskState build() {
    ref.listen<String?>(accountDbKeyProvider, (prev, next) {
      if (prev != next) {
        _accountGeneration++;
        if (prev != null &&
            prev.isNotEmpty &&
            state.activeRecordingIds.isNotEmpty) {
          _interruptedIdsByAccountKey
              .putIfAbsent(prev, () => <String>{})
              .addAll(state.activeRecordingIds);
        }
        for (final id in state.activeRecordingIds) {
          _cancelRequested[id] = true;
        }
        state = const TranscriptionTaskState();
        // Login can land directly on the detail page, without rebuilding the
        // recordings list that normally triggers reconciliation. Reconcile the
        // newly selected account shard as soon as its DB becomes available.
        if (next != null && next.isNotEmpty) {
          Future<void>.microtask(() => _recoverAfterAccountLogin(next));
        }
      }
    });
    return const TranscriptionTaskState();
  }

  Future<void> _recoverAfterAccountLogin(String accountKey) async {
    final interrupted = _interruptedIdsByAccountKey.remove(accountKey);
    if (interrupted != null && interrupted.isNotEmpty) {
      try {
        final repo = await ref.read(recordingsRepositoryProvider.future);
        for (final id in interrupted) {
          final rec = await repo.getById(id);
          // Logout is a hard ownership boundary: never continue an old account's
          // job after another login, even when the server job id still exists.
          // Make every interrupted row explicitly retryable.
          if (rec != null) {
            await repo.updateJobState(id, 'none');
            _publish(id);
          }
        }
      } catch (e, st) {
        if (kDebugMode) {
          debugPrint('[ASR login recovery] account=$accountKey: $e\n$st');
        }
      }
    }
    await reconcileStuckJobs();
  }

  void cancel(String recordingId) {
    _cancelRequested[recordingId.trim()] = true;
  }

  void _registerActive(
    String recordingId, {
    required int generation,
    String? message,
  }) {
    final id = recordingId.trim();
    _cancelRequested.remove(id);
    _activeTaskGeneration[id] = generation;
    final progress = Map<String, String>.from(state.progressByRecordingId);
    final trimmed = message?.trim() ?? '';
    if (trimmed.isNotEmpty) progress[id] = trimmed;
    state = TranscriptionTaskState(
      progressByRecordingId: progress,
      activeRecordingIds: {...state.activeRecordingIds, id},
    );
  }

  void _unregisterActive(String recordingId, {int? generation}) {
    final id = recordingId.trim();
    if (generation != null && _activeTaskGeneration[id] != generation) {
      return;
    }
    _activeTaskGeneration.remove(id);
    final nextProgress = Map<String, String>.from(state.progressByRecordingId)
      ..remove(id);
    final nextActive = Set<String>.from(state.activeRecordingIds)..remove(id);
    _cancelRequested.remove(id);
    state = TranscriptionTaskState(
      progressByRecordingId: nextProgress,
      activeRecordingIds: nextActive,
    );
  }

  void _setProgress(String recordingId, String message, {int? generation}) {
    final id = recordingId.trim();
    if (message.trim().isEmpty) return;
    if (generation != null &&
        (_accountGeneration != generation ||
            _activeTaskGeneration[id] != generation)) {
      return;
    }
    state = TranscriptionTaskState(
      progressByRecordingId: {...state.progressByRecordingId, id: message},
      activeRecordingIds: state.activeRecordingIds,
    );
  }

  /// Runs the full local-audio transcription pipeline. Survives detail-page dispose
  /// unless [cancel] is called for this recording.
  Future<String?> run({
    required Recording recording,
    required int asrResultId,
    required TranscribeSheetSelection selection,
    BuildContext? uiContext,
    bool allowGatewayTimeoutRetry = true,
    Future<void> Function()? onGatewayTimeoutRetry,
  }) async {
    final id = recording.id.trim();
    if (state.isActive(id)) return null;

    final generation = _accountGeneration;
    final locale = ref.read(appLocaleProvider);
    final l10n = AppLocalizations(locale);
    _registerActive(id, generation: generation, message: l10n.transcribing);
    final progressNotifier = ValueNotifier<String>(l10n.transcribing);
    void onProgressChanged() {
      final msg = progressNotifier.value;
      if (msg.isNotEmpty) _setProgress(id, msg, generation: generation);
    }

    progressNotifier.addListener(onProgressChanged);
    try {
      return await runRecordingTranscriptionPipeline(
        ref: ref,
        l10n: l10n,
        locale: locale,
        uiContext: uiContext,
        recording: recording,
        asrResultId: asrResultId,
        selection: selection,
        progressMessage: progressNotifier,
        shouldAbort: () =>
            generation != _accountGeneration || _cancelRequested[id] == true,
        allowGatewayTimeoutRetry: allowGatewayTimeoutRetry,
        onGatewayTimeoutRetry: onGatewayTimeoutRetry,
      );
    } finally {
      progressNotifier.removeListener(onProgressChanged);
      progressNotifier.dispose();
      _unregisterActive(id, generation: generation);
    }
  }

  /// Poll server jobs for rows stuck in `queued/transcribing` without a live client task.
  Future<void> reconcileStuckJobs() async {
    if (_reconcileInFlight) return;
    _reconcileInFlight = true;
    try {
      await withFreshRecordingsRepo(ref, (repo) async {
        final stuck = await repo.listTranscriptionJobsInProgress();
        if (stuck.isEmpty) return;

        final asrApi = ref.read(asrApiProvider);
        for (final rec in stuck) {
          final hasTranscript = (rec.transcript ?? '').trim().isNotEmpty;
          if (state.isActive(rec.id)) {
            // A live run always publishes a progress string with the active
            // flag. Active with no message is a leftover lock.
            if (hasTranscript && state.progressFor(rec.id) == null) {
              _unregisterActive(rec.id);
            } else {
              continue;
            }
          }
          // A row that already has text is a finished result, not a live job.
          // Polling it would keep the detail page on "transcribing".
          if (hasTranscript) {
            await repo.updateJobState(rec.id, 'done');
            _publish(rec.id);
            continue;
          }
          await _reconcileRecording(rec, repo, asrApi);
        }
      });
    } finally {
      _reconcileInFlight = false;
    }
  }

  Future<void> _reconcileRecording(
    Recording rec,
    RecordingsRepository repo,
    AsrApi asrApi,
  ) async {
    final jobIdStr = rec.lastSttJobId?.trim();
    if (jobIdStr == null || jobIdStr.isEmpty) {
      final hasTranscript = (rec.transcript ?? '').trim().isNotEmpty;
      await repo.updateJobState(rec.id, hasTranscript ? 'done' : 'none');
      _publish(rec.id);
      return;
    }

    final jobId = int.tryParse(jobIdStr);
    if (jobId == null || jobId <= 0) {
      await repo.updateJobState(rec.id, 'none');
      _publish(rec.id);
      return;
    }

    try {
      final job = await asrApi.getJobById(jobId);
      if (job.isRunning || job.isQueued) {
        unawaited(_resumeServerJobPoll(rec, jobId));
      } else if (job.isSucceeded) {
        await _finishFromServerJob(rec, job, asrApi, repo);
      } else if (job.isFailed) {
        await repo.updateJobState(rec.id, 'failed');
        _publish(rec.id);
      }
    } catch (e, st) {
      if (kDebugMode) {
        debugPrint('[ASR reconcile] ${rec.id} job=$jobId: $e\n$st');
      }
    }
  }

  Future<void> _resumeServerJobPoll(Recording rec, int jobId) async {
    if (state.isActive(rec.id)) return;
    final generation = _accountGeneration;
    final l10n = AppLocalizations(ref.read(appLocaleProvider));
    _registerActive(
      rec.id,
      generation: generation,
      message: l10n.transcribing,
    );
    try {
      final asrApi = ref.read(asrApiProvider);
      final repo = await ref.read(recordingsRepositoryProvider.future);

      final finishedJob = await asrApi.waitJobUntilDone(
        jobId,
        onJobUpdate: (job) async {
          if (generation != _accountGeneration ||
              _activeTaskGeneration[rec.id] != generation) {
            return;
          }
          final nextState = switch (job.status) {
            'pending' => 'queued',
            'running' => 'transcribing',
            'failed' => 'failed',
            'succeeded' => 'transcribing',
            _ => 'transcribing',
          };
          await repo.updateJobState(
            rec.id,
            nextState,
            sttJobId: job.id.toString(),
          );
          _publish(rec.id);
        },
      );

      if (generation != _accountGeneration ||
          _activeTaskGeneration[rec.id] != generation) {
        return;
      }
      if (finishedJob.isSucceeded) {
        await _finishFromServerJob(rec, finishedJob, asrApi, repo);
      } else {
        await repo.updateJobState(rec.id, 'failed');
        _publish(rec.id);
      }
    } catch (e, st) {
      if (kDebugMode) {
        debugPrint('[ASR resume poll] ${rec.id}: $e\n$st');
      }
      if (generation == _accountGeneration &&
          _activeTaskGeneration[rec.id] == generation) {
        try {
          final repo = await ref.read(recordingsRepositoryProvider.future);
          await repo.updateJobState(rec.id, 'failed');
          _publish(rec.id);
        } catch (_) {
          // The account may have been invalidated while handling the error.
        }
      }
    } finally {
      _unregisterActive(rec.id, generation: generation);
    }
  }

  Future<void> _finishFromServerJob(
    Recording rec,
    AsrJobItem job,
    AsrApi asrApi,
    RecordingsRepository repo,
  ) async {
    final resultId = job.asrResultId;
    if (resultId == null || resultId <= 0) {
      await repo.updateJobState(rec.id, 'failed');
      _publish(rec.id);
      return;
    }
    final locale = ref.read(appLocaleProvider);
    final langRes = resolvedTranscribeLanguage(
      (rec.lastLanguage ?? '').trim().isEmpty ? 'Auto' : rec.lastLanguage!,
      locale,
    );
    final result = await asrApi.getResultByIdWithRetry(resultId);
    final text = result.displayText(languageHint: langRes.displayHint);
    await repo.updateTranscript(rec.id, text);
    await repo.updateJobState(rec.id, 'done');
    _publish(rec.id);
  }

  void _publish(String recordingId) {
    ref.invalidate(recordingByIdProvider(recordingId));
    bumpRecordingsLists(ref);
  }
}
