import Foundation
import Speech
import AVFoundation

@MainActor
@Observable
final class VoiceInputService {
    private(set) var isRecording = false
    private(set) var transcript = ""
    private(set) var authStatus: SFSpeechRecognizerAuthorizationStatus = .notDetermined
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
        let speechStatus = await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0) }
        }
        authStatus = speechStatus

        // Microphone permission — iOS 17 API path when available
        if #available(iOS 17.0, *) {
            _ = await AVAudioApplication.requestRecordPermission()
        } else {
            _ = await withCheckedContinuation { cont in
                AVAudioSession.sharedInstance().requestRecordPermission { cont.resume(returning: $0) }
            }
        }
    }

    func startRecording() {
        guard !isRecording else { return }
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

        inputNode.removeTap(onBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.request?.append(buffer)
        }

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
}
