package runner

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestRefreshLeaseUsesExplicitEndpointAndRequiresSameToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/remote/token/refresh" {
			http.NotFound(w, req)
			return
		}
		var refresh protocol.TokenRefreshRequest
		if err := json.NewDecoder(req.Body).Decode(&refresh); err != nil {
			t.Fatal(err)
		}
		_ = json.NewEncoder(w).Encode(protocol.TokenRefreshResponse{
			OK: true, PeerToken: refresh.PeerToken, ExpiresInSec: 60,
		})
	}))
	defer server.Close()
	r := &Runner{client: client.New(server.URL)}

	if err := r.refreshLease(context.Background(), "pt_demo"); err != nil {
		t.Fatal(err)
	}
}
