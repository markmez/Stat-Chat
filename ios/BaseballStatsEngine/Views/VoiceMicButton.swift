import SwiftUI

/// Reusable mic-affordance button. Toggles between an idle mic icon (tappable to
/// start dictation) and a pulsing red stop icon (tappable to stop). Used inline
/// in search/input fields throughout the app for consistent voice input UX.
struct VoiceMicButton: View {
    @Bindable var voice: VoiceInputService
    let tint: Color
    var onStart: (() -> Void)? = nil
    @Environment(\.colorScheme) private var colorScheme
    @State private var ringPulse: Bool = false

    var body: some View {
        if voice.isRecording {
            Button {
                voice.stopRecording()
            } label: {
                ZStack {
                    Circle()
                        .stroke(tint.opacity(0.7), lineWidth: 2)
                        .scaleEffect(ringPulse ? 1.5 : 1.0)
                        .opacity(ringPulse ? 0 : 0.9)
                    Image(systemName: "stop.fill")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(tint)
                        .frame(width: 32, height: 32)
                        .background(colorScheme == .dark ? Color(.systemGray5) : Color(.tertiarySystemFill), in: Circle())
                }
                .frame(width: 32, height: 32)
            }
            .accessibilityLabel("Stop recording")
            .onAppear {
                ringPulse = false
                withAnimation(.easeOut(duration: 1.2).repeatForever(autoreverses: false)) {
                    ringPulse = true
                }
            }
            .onDisappear {
                ringPulse = false
            }
        } else {
            Button {
                Task { @MainActor in
                    if voice.authStatus == .notDetermined {
                        await voice.requestAuthorization()
                    }
                    guard voice.authStatus == .authorized, voice.micGranted else { return }
                    onStart?()
                    voice.startRecording()
                }
            } label: {
                Image(systemName: "mic.fill")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(tint)
                    .frame(width: 32, height: 32)
                    .background(colorScheme == .dark ? Color(.systemGray5) : Color(.tertiarySystemFill), in: Circle())
            }
            .accessibilityLabel("Voice search")
        }
    }
}
