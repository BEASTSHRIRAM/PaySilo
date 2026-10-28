import { NextResponse, type NextRequest } from "next/server";

// Cheap gate: no session cookie -> straight to login. The backend still
// validates the token on every API call; this only avoids a flash of the app.
export function middleware(request: NextRequest) {
  if (!request.cookies.has("paysilo_session")) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = `?next=${encodeURIComponent(request.nextUrl.pathname)}`;
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/dashboard/:path*"] };
