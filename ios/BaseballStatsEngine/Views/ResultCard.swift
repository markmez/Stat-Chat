import SwiftUI

struct ResultCard: View {
    let message: Message

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

    // User question — minimal, just the query text
    private var userQuery: some View {
        HStack {
            Text(message.content)
                .font(.system(.title3, design: .rounded, weight: .semibold))
                .foregroundStyle(.white)
            Spacer()
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
    }

    // Answer — rich card with translucent background
    private var answerCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            if message.content.isEmpty {
                // Streaming hasn't started yet
                Text(" ")
                    .font(.system(.body, design: .rounded))
            } else {
                Text(LocalizedStringKey(message.content))
                    .font(.system(.body, design: .rounded))
                    .foregroundStyle(.white.opacity(0.9))
                    .textSelection(.enabled)
                    .lineSpacing(3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(18)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(.white.opacity(0.06))
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(.white.opacity(0.08), lineWidth: 0.5)
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
