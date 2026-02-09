import SwiftUI

struct APIKeySetupView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var apiKeyText = ""
    @State private var showError = false
    @State private var errorMessage = ""

    let isInitialSetup: Bool

    private let lightBlue = Color(red: 0.45, green: 0.7, blue: 1.0)

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()

            VStack(spacing: 28) {
                Spacer()

                VStack(spacing: 10) {
                    Image(systemName: "key.fill")
                        .font(.system(size: 36))
                        .foregroundStyle(lightBlue.opacity(0.6))

                    Text("API Key")
                        .font(.system(.title2, design: .rounded, weight: .bold))
                        .foregroundStyle(.primary)

                    Text("Enter your Anthropic API key.\nStored in the iOS Keychain.")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }

                SecureField("", text: $apiKeyText, prompt:
                    Text("sk-ant-...")
                        .foregroundStyle(.tertiary)
                )
                .font(.system(.body, design: .monospaced))
                .foregroundStyle(.primary)
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(Color(uiColor: .separator).opacity(0.3), lineWidth: 0.5)
                )
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .padding(.horizontal, 24)

                if showError {
                    Text(errorMessage)
                        .font(.system(.caption, design: .rounded))
                        .foregroundStyle(.red.opacity(0.9))
                }

                Button(action: saveKey) {
                    Text("Save")
                        .font(.system(.headline, design: .rounded))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 14)
                        .background(lightBlue, in: RoundedRectangle(cornerRadius: 12))
                }
                .padding(.horizontal, 24)
                .disabled(apiKeyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .opacity(apiKeyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.4 : 1.0)

                if !isInitialSetup, KeychainHelper.load() != nil {
                    Button("Delete Key", role: .destructive) {
                        _ = KeychainHelper.delete()
                        appState.refreshAPIKeyStatus()
                        dismiss()
                    }
                    .font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(.red.opacity(0.7))
                }

                Spacer()
                Spacer()
            }
        }
        .navigationTitle(isInitialSetup ? "" : "Settings")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.automatic, for: .navigationBar)
    }

    private func saveKey() {
        let trimmed = apiKeyText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        if KeychainHelper.save(apiKey: trimmed) {
            apiKeyText = ""
            appState.refreshAPIKeyStatus()
            if !isInitialSetup {
                dismiss()
            }
        } else {
            showError = true
            errorMessage = "Failed to save key to Keychain."
        }
    }
}
