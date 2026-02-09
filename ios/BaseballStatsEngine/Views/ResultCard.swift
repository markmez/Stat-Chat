import SwiftUI

struct ResultCard: View {
    let message: Message
    var isFirstUser: Bool = false
    var onBack: (() -> Void)? = nil

    private let deepBlue = Color(red: 0.1, green: 0.25, blue: 0.7)

    var body: some View {
        switch message.role {
        case .user:
            userQuery

        case .assistant:
            answerCard

        case .error:
            errorCard
        }
    }

    // User question — with back chevron on the first one
    private var userQuery: some View {
        HStack(alignment: .top, spacing: 10) {
            if isFirstUser, let onBack {
                Button(action: onBack) {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 22, weight: .medium))
                        .foregroundStyle(Color(red: 0.45, green: 0.7, blue: 1.0))
                }
                .padding(.top, 2)
            }

            Text(message.content)
                .font(.system(.title3, design: .rounded, weight: .semibold))
                .foregroundStyle(.primary)

            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
    }

    // Answer — rich card with subtle background
    private var answerCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            if message.content.isEmpty {
                // Streaming hasn't started yet
                Text(" ")
                    .font(.system(.body, design: .rounded))
            } else {
                Text(LocalizedStringKey(message.content))
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.primary.opacity(0.85))
                    .textSelection(.enabled)
                    .lineSpacing(3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(18)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color(uiColor: .secondarySystemBackground))
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(Color(uiColor: .separator).opacity(0.2), lineWidth: 0.5)
                )
        )
        .padding(.horizontal, 16)
    }

    // Error
    private var errorCard: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 14, weight: .medium))
            Text(message.content)
                .font(.system(.callout, design: .rounded))
        }
        .foregroundStyle(.red.opacity(0.9))
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(.red.opacity(0.08))
                .overlay(
                    RoundedRectangle(cornerRadius: 12)
                        .stroke(.red.opacity(0.15), lineWidth: 0.5)
                )
        )
        .padding(.horizontal, 16)
    }
}
