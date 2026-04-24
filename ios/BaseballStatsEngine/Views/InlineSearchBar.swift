import SwiftUI

/// Shared inline search bar used by ResultsView (follow-up input),
/// PlayerCardView, and TeamCardView. Single source of truth for layout,
/// styling, and voice-input integration on the non-Home surfaces.
///
/// HomeView has a custom ZStack-based card with `ExclusionTextView` for
/// text-wraps-around-mic — it does NOT use this component.
struct InlineSearchBar: View {
    @Binding var text: String
    var placeholder: String
    @FocusState.Binding var isFocused: Bool
    @Bindable var voice: VoiceInputService
    @Binding var voiceUsedThisQuery: Bool
    var onSubmit: () -> Void
    var tint: Color
    var deepBlue: Color

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(tint)

            TextField("", text: $text, prompt:
                Text(placeholder)
                    .foregroundColor(Color(.label).opacity(0.33)),
                axis: .vertical
            )
            .font(.system(.body, design: .rounded))
            .foregroundStyle(.primary)
            .lineLimit(1...3)
            .focused($isFocused)
            .autocorrectionDisabled(true)
            .textInputAutocapitalization(.never)
            .submitLabel(.search)
            .onSubmit { onSubmit() }
            .onChange(of: text) { _, newValue in
                if newValue.contains("\n") {
                    text = newValue.replacingOccurrences(of: "\n", with: "")
                    onSubmit()
                }
            }

            if !text.isEmpty {
                Button(action: onSubmit) {
                    Image(systemName: "arrow.right.circle.fill")
                        .font(.system(size: 22))
                        .foregroundStyle(tint)
                }
            }

            VoiceMicButton(voice: voice, tint: tint) {
                voiceUsedThisQuery = true
            }
        }
        .onChange(of: voice.transcript) { _, new in
            if !new.isEmpty { text = new }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
        .shadow(color: deepBlue.opacity(0.12), radius: 12, y: 4)
        .shadow(color: .black.opacity(0.04), radius: 2, y: 1)
    }
}
