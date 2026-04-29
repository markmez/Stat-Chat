import Foundation
import Speech
import AVFoundation

@MainActor
@Observable
final class VoiceInputService {
    /// Why the most recent recording stopped. Callers use this to decide
    /// whether to auto-submit the transcript: only `.silence` should
    /// auto-submit (the user trailed off — clear "I'm done" intent).
    /// Manual stops mean "let me edit"; hard-cap stops mean truncation;
    /// errors should never auto-submit.
    enum StopReason { case manual, silence, hardCap, error }

    private(set) var isRecording = false
    private(set) var transcript = ""
    private(set) var authStatus: SFSpeechRecognizerAuthorizationStatus = .notDetermined
    private(set) var micGranted: Bool = false
    private(set) var errorMessage: String?
    private(set) var lastStopReason: StopReason?

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let audioEngine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silenceTimer: Timer?
    private var hardCapTimer: Timer?
    /// Trail-off detection. 1.5s mirrors Apple's keyboard-dictation feel.
    /// Started ONLY after the first partial result lands so the user can
    /// take a beat to think before speaking without getting cut off.
    private let silenceTimeout: TimeInterval = 1.5
    /// Absolute cap so a stuck mic / forgotten session can't run forever.
    private let hardCapTimeout: TimeInterval = 30.0

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

        // Build contextualStrings once — passed to SFSpeechRecognizer as
        // hints for terms likely to be spoken. Significantly improves
        // accuracy on baseball-specific names the generic recognizer
        // mangles (Acuña, Semien, Ohtani, etc.). Apple recommends staying
        // under ~100 entries.
        if contextualStrings.isEmpty {
            // 70 current MLB stars — picked by award count since 2020 (MVPs,
            // Cy Youngs, All-Stars, Silver Sluggers, Gold Gloves) then
            // filtered to names worth optimizing for recognition: foreign
            // origin, accented, unusual spelling, or compound surnames.
            // Common English names (Trout, Judge, Harper, Freeman, etc.)
            // are excluded because the default recognizer handles them.
            let currentStars = [
                "Shohei Ohtani", "Juan Soto", "Mookie Betts", "Vladimir Guerrero Jr.",
                "Fernando Tatís Jr.", "Marcus Semien", "Ronald Acuña Jr.", "Nolan Arenado",
                "Bobby Witt Jr.", "Julio Rodríguez", "José Ramírez", "Manny Machado",
                "Steven Kwan", "Corey Seager", "Salvador Perez", "Pete Alonso",
                "Teoscar Hernández", "Rafael Devers", "Corbin Burnes", "Luis Arraez",
                "Yordan Alvarez", "Jose Altuve", "Tarik Skubal", "Kyle Schwarber",
                "Ketel Marte", "Randy Arozarena", "Andrés Giménez", "William Contreras",
                "Dansby Swanson", "Paul Goldschmidt", "Xander Bogaerts", "Paul Skenes",
                "Brent Rooker", "Carlos Rodón", "Francisco Lindor", "Alejandro Kirk",
                "Clayton Kershaw", "Jazz Chisholm", "Byron Buxton", "Adley Rutschman",
                "Gunnar Henderson", "Emmanuel Clase", "Luis Robert Jr.", "Adolis García",
                "Nick Castellanos", "Ozzie Albies", "J.T. Realmuto", "Freddy Peralta",
                "Nico Hoerner", "Ke'Bryan Hayes", "Mauricio Dubón", "Elly De La Cruz",
                "Jacob deGrom", "Garrett Crochet", "Wilyer Abreu", "Anthony Santander",
                "Marcell Ozuna", "Yandy Díaz", "Luis Castillo", "Yoshinobu Yamamoto",
                "Roki Sasaki", "Sandy Alcantara", "Edwin Díaz", "Bo Bichette",
                "Carlos Correa", "Kodai Senga", "Yu Darvish", "Seiya Suzuki",
                "Shōta Imanaga", "Cole Ragans",
            ]
            // 30 all-time legends — top historical award counts, same filter
            let legends = [
                "Babe Ruth", "Lou Gehrig", "Mickey Mantle", "Joe DiMaggio",
                "Stan Musial", "Honus Wagner", "Sandy Koufax", "Pedro Martinez",
                "Greg Maddux", "Ken Griffey Jr.", "Albert Pujols", "Cal Ripken Jr.",
                "Tony Gwynn", "Roberto Clemente", "Mariano Rivera", "Ichiro Suzuki",
                "Yogi Berra", "Iván Rodríguez", "Mike Piazza", "Yadier Molina",
                "David Ortiz", "Carl Yastrzemski", "Vladimir Guerrero", "Roberto Alomar",
                "Manny Ramirez", "Miguel Cabrera", "Luis Aparicio", "Carlton Fisk",
                "Mark McGwire", "Hideki Matsui",
            ]
            let teams = ["Yankees", "Red Sox", "Dodgers", "Mets", "Giants",
                         "Braves", "Cubs", "Cardinals", "Astros", "Rangers",
                         "Phillies", "Blue Jays", "Orioles", "Tigers", "Pirates",
                         "Guardians", "Twins", "Royals", "White Sox", "Mariners",
                         "Angels", "Athletics", "Padres", "Rockies", "Diamondbacks",
                         "Nationals", "Marlins", "Rays", "Brewers", "Reds"]
            let stats = ["OPS", "OPS plus", "ERA", "ERA plus", "WHIP", "RBI",
                         "strikeouts", "home runs", "batting average", "stolen bases"]
            contextualStrings = currentStars + legends + teams + stats
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
        lastStopReason = nil
        // Silence timer is NOT started here — only after the first partial
        // result lands. Otherwise a thoughtful user pausing before they
        // start speaking would get cut off after silenceTimeout seconds.
        startHardCapTimer()

        task = recognizer?.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            Task { @MainActor in
                if let result {
                    self.transcript = result.bestTranscription.formattedString
                    self.resetSilenceTimer()
                }
                if let error {
                    self.errorMessage = error.localizedDescription
                    self.stopRecording(reason: .error)
                }
                if result?.isFinal == true {
                    self.stopRecording(reason: .silence)
                }
            }
        }
    }

    private func resetSilenceTimer() {
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: silenceTimeout, repeats: false) { [weak self] _ in
            Task { @MainActor in
                self?.stopRecording(reason: .silence)
            }
        }
    }

    private func startHardCapTimer() {
        hardCapTimer?.invalidate()
        hardCapTimer = Timer.scheduledTimer(withTimeInterval: hardCapTimeout, repeats: false) { [weak self] _ in
            Task { @MainActor in
                self?.stopRecording(reason: .hardCap)
            }
        }
    }

    /// Public stop (mic-button tap). Marks the stop as manual so callers
    /// don't auto-submit the transcript — the user wants to review/edit.
    func stopRecording() {
        stopRecording(reason: .manual)
    }

    private func stopRecording(reason: StopReason) {
        guard isRecording else { return }
        isRecording = false
        lastStopReason = reason
        silenceTimer?.invalidate()
        silenceTimer = nil
        hardCapTimer?.invalidate()
        hardCapTimer = nil
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
