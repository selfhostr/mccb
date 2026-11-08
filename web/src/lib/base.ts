/** A file of web/public, under the base of the build ('/' in the product, '/demo/' in the static demo). */
export const asset = (name: string): string => `${import.meta.env.BASE_URL}${name}`;

/** A page of the documentation as the project site serves it (docs/<name>.md -> /docs/<name>/). */
export const docsUrl = (name: string): string => `https://mccb.selfhostr.io/docs/${name}/`;
