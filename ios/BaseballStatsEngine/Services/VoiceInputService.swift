import Foundation
import Speech
import AVFoundation

@MainActor
@Observable
final class VoiceInputService {
    private(set) var isRecording = false
    private(set) var transcript = ""
    private(set) var authStatus: SFSpeechRecognizerAuthorizationStatus = .notDetermined
    private(set) var micGranted: Bool = false
    private(set) var errorMessage: String?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let audioEngine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?

    // Player-name context hints for recognizer. Built lazily on first use from
    // PlayerNameMatcher.sortedNames — helps recognition accuracy on names like
    // Semien, Adames, Acuña, Realmuto that Apple's recognizer often mangles.
    private var contextualStrings: [String] = []

    func requestAuthorization() async {
        // Permission APIs deliver callbacks on background threads. Wrapping
        // them via withCheckedContinuation inside a @MainActor function
        // crashes under Swift 6 strict concurrency
        // (`_swift_task_checkIsolatedSwift` assertion in __TCCAccessRequest
        // callback) because the closure inherits MainActor isolation that
        // Apple's API can't honor. Use non-isolated static helpers so the
        // closures have no actor expectation; we hop back to the main
        // actor automatically after each await.
        authStatus = await Self._requestSpeechAuth()
        micGranted = await Self._requestMicAuth()
    }

    nonisolated private static func _requestSpeechAuth() async -> SFSpeechRecognizerAuthorizationStatus {
        await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { status in
                cont.resume(returning: status)
            }
        }
    }

    nonisolated private static func _requestMicAuth() async -> Bool {
        await withCheckedContinuation { cont in
            AVAudioSession.sharedInstance().requestRecordPermission { granted in
                cont.resume(returning: granted)
            }
        }
    }

    func startRecording() {
        guard !isRecording else { return }
        guard authStatus == .authorized else {
            errorMessage = "Speech recognition not authorized"
            return
        }
        guard micGranted else {
            errorMessage = "Microphone access not granted"
            return
        }
        errorMessage = nil
        transcript = ""

        // Build contextualStrings once. Keep under ~100 per Apple guidance.
        if contextualStrings.isEmpty {
            // Start with top player names (first in sortedNames = longest names;
            // not prominence-ordered, but that's the current source of truth).
            // Also include common team names + core stats to improve recognition.
            let playerNames = Array(PlayerNameMatcher.sortedNames.prefix(60))
            let teams = ["Yankees", "Red Sox", "Dodgers", "Mets", "Giants",
                         "Braves", "Cubs", "Cardinals", "Astros", "Rangers",
                         "Phillies", "Blue Jays", "Orioles", "Tigers", "Pirates",
                         "Guardians", "Twins", "Royals", "White Sox", "Mariners",
                         "Angels", "Athletics", "Padres", "Rockies", "Diamondbacks",
                         "Nationals", "Marlins", "Rays", "Brewers", "Reds"]
            let stats = ["OPS", "OPS plus", "ERA", "ERA plus", "WHIP", "RBI",
                         "strikeouts", "home runs", "batting average", "stolen bases"]
            contextualStrings = playerNames + teams + stats
        }

        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .measurement, options: .duckOthers)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            errorMessage = "Audio session error: \(error.localizedDescription)"
            return
        }

        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.contextualStrings = contextualStrings
        if recognizer?.supportsOnDeviceRecognition == true {
            req.requiresOnDeviceRecognition = true
        }
        self.request = req

        let inputNode = audioEngine.inputNode
        let format = inputNode.outputFormat(forBus: 0)

        // Defensive: installTap crashes the app with an assertion failure
        // if format has 0 channels or 0 sample rate (can happen if mic
        // permission was just granted and the audio system hasn't finished
        // wiring up the input node yet).
        guard format.channelCount > 0, format.sampleRate > 0 else {
            errorMessage = "Audio input not available — try again"
            teardown()
            return
        }

        inputNode.removeTap(onBus: 0)
        // The tap callback runs on AVAudioEngine's realtime audio thread.
        // We CANNOT capture `self` here (Swift 6 strict concurrency
        // would mark the closure as MainActor-isolated and crash the
        // app at runtime when the audio thread invokes it). Capture the
        // request directly via a nonisolated helper.
        Self._installTap(on: inputNode, bufferSize: 1024, format: format, request: req)

        audioEngine.prepare()
        do {
            try audioEngine.start()
        } catch {
            errorMessage = "Audio engine error: \(error.localizedDescription)"
            teardown()
            return
        }

        isRecording = true

        task = recognizer?.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            Task { @MainActor in
                if let result {
                    self.transcript = result.bestTranscription.formattedString
                }
                if let error {
                    self.errorMessage = error.localizedDescription
                    self.stopRecording()
                }
                if result?.isFinal == true {
                    self.stopRecording()
                }
            }
        }
    }

    func stopRecording() {
        guard isRecording else { return }
        isRecording = false
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        teardown()
    }

    private func teardown() {
        request = nil
        task = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Install an audio buffer tap from a non-isolated context. Required
    /// because AVAudioEngine invokes the buffer callback on a realtime
    /// audio thread; a closure declared inside this @MainActor class
    /// would inherit MainActor isolation and crash with
    /// `_swift_task_checkIsolatedSwift` when the audio thread invokes it.
    nonisolated private static func _installTap(
        on inputNode: AVAudioInputNode,
        bufferSize: AVAudioFrameCount,
        format: AVAudioFormat,
        request: SFSpeechAudioBufferRecognitionRequest
    ) {
        inputNode.installTap(onBus: 0, bufferSize: bufferSize, format: format) { buffer, _ in
            request.append(buffer)
        }
    }
}
